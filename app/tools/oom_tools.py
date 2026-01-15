"""
OOM 분석을 위한 LangChain Tools
"""

import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import boto3
from langchain_core.tools import tool

SCRIPTS_PATH = (
    Path(__file__).parent.parent.parent
    / ".claude"
    / "skills"
    / "oom-analyzer"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_PATH))

from list_log_streams import list_streams, format_time
from find_incomplete_requests import (
    get_stream_last_timestamp,
    fetch_logs_from_cloudwatch,
    parse_log_message,
    extract_request_id,
    is_started_request,
    is_completed_request,
    extract_request_info,
    should_exclude_path,
)

# 설정
CLOUDWATCH_LOG_GROUP = (
    "/aws/containerinsights/ped-eks-cluster-v2-service-all-prd/application"
)
AWS_REGION = "ap-northeast-2"


def _get_cloudwatch_client():
    """CloudWatch Logs 클라이언트 생성"""
    return boto3.client("logs", region_name=AWS_REGION)


@tool
def list_log_streams(
    pod_name: Annotated[
        str,
        "분석할 pod 이름 (예: class-rails-service-apne2-prd-756c9bf4ff-hz66f)",
    ],
) -> str:
    """
    CloudWatch에서 특정 pod의 로그 스트림 목록을 조회합니다.

    OOM 분석의 첫 번째 단계로, 이 도구를 사용하여 분석할 로그 스트림을 선택합니다.
    Last 시간이 OOM 발생 시간과 가장 가까운 스트림을 선택하세요.

    반환값: 로그 스트림 목록 (이름, First 시간, Last 시간)
    """
    client = _get_cloudwatch_client()

    try:
        streams = list_streams(client, CLOUDWATCH_LOG_GROUP, pod_name)

        if not streams:
            return f"'{pod_name}'와 일치하는 로그 스트림을 찾을 수 없습니다."

        # 결과 포맷팅
        result_lines = [f"'{pod_name}' 로그 스트림 ({len(streams)}개):\n"]
        for stream in streams:
            result_lines.append(f"스트림: {stream['name']}")
            first_str = format_time(stream["first_event_time"])
            last_str = format_time(stream["last_event_time"])
            result_lines.append(f"  First: {first_str}  |  Last: {last_str}")
            result_lines.append("")

        return "\n".join(result_lines)

    except Exception as e:
        return f"로그 스트림 조회 중 오류 발생: {str(e)}"


@tool
def find_incomplete_requests(
    log_stream: Annotated[
        str,
        "분석할 로그 스트림 이름 (list_log_streams 결과에서 선택)",
    ],
    minutes_before: Annotated[
        int,
        "OOM 발생 전 몇 분간의 로그를 분석할지 (기본값: 5)",
    ] = 5,
) -> str:
    """
    CloudWatch 로그에서 미완료 요청을 찾습니다.

    Started는 있지만 Completed가 없는 요청을 찾아 OOM 원인을 파악합니다.
    이 도구는 list_log_streams로 선택한 로그 스트림을 분석합니다.

    반환값: 미완료 요청 목록 (경로별 그룹화, IP, 요청 ID)
    """
    client = _get_cloudwatch_client()

    try:
        last_timestamp = get_stream_last_timestamp(
            client, CLOUDWATCH_LOG_GROUP, log_stream
        )

        if not last_timestamp:
            return f"로그 스트림 '{log_stream}'을 찾을 수 없거나 이벤트가 없습니다."

        end_time = datetime.fromtimestamp(last_timestamp / 1000)
        start_time = end_time - timedelta(minutes=minutes_before)

        start_time_ms = int(start_time.timestamp() * 1000)
        end_time_ms = int(end_time.timestamp() * 1000)

        raw_logs = fetch_logs_from_cloudwatch(
            client, CLOUDWATCH_LOG_GROUP, log_stream, start_time_ms, end_time_ms
        )

        if not raw_logs:
            return "지정된 시간 범위에서 로그를 찾을 수 없습니다."

        started_requests = {}
        completed_requests = set()

        for log_entry in raw_logs:
            parsed = parse_log_message(log_entry["message"])
            if not parsed:
                continue

            log_line = parsed["log"]
            timestamp = parsed["timestamp"]

            request_id = extract_request_id(log_line)
            if not request_id:
                continue

            if is_started_request(log_line):
                request_info = extract_request_info(log_line)
                if request_info:
                    started_requests[request_id] = {
                        "info": request_info,
                        "timestamp": timestamp,
                    }
            elif is_completed_request(log_line):
                completed_requests.add(request_id)

        # 미완료 요청 찾기
        incomplete_requests = []
        for request_id, request_data in started_requests.items():
            if request_id not in completed_requests:
                path = request_data["info"]["path"]
                if should_exclude_path(path):
                    continue
                incomplete_requests.append(
                    {
                        "request_id": request_id,
                        "method": request_data["info"]["method"],
                        "path": path,
                        "ip": request_data["info"]["ip"],
                        "timestamp": request_data["timestamp"],
                    }
                )

        # 결과 포맷팅
        result_lines = [
            "=" * 60,
            "분석 결과",
            "=" * 60,
            f"분석 구간: {start_time.isoformat()} ~ {end_time.isoformat()}",
            f"총 로그 라인: {len(raw_logs)}",
            f"시작된 요청: {len(started_requests)}",
            f"완료된 요청: {len(completed_requests)}",
            f"미완료 요청: {len(incomplete_requests)}",
            "",
        ]

        if incomplete_requests:
            # 경로별 그룹화
            by_path = defaultdict(list)
            for req in incomplete_requests:
                by_path[req["path"]].append(req)

            result_lines.append("=" * 60)
            result_lines.append("미완료 요청 (Started는 있지만 Completed가 없음)")
            result_lines.append("=" * 60)

            for path, reqs in sorted(
                by_path.items(), key=lambda x: len(x[1]), reverse=True
            ):
                result_lines.append(f"\n[{len(reqs)}x] {reqs[0]['method']} {path}")
                unique_ips = list(set(req["ip"] for req in reqs))
                result_lines.append(f"  IPs: {', '.join(unique_ips[:5])}")
                if len(unique_ips) > 5:
                    result_lines.append(f"       ... 외 {len(unique_ips) - 5}개 IP")
                result_lines.append("  Request IDs:")
                for req in reqs[:3]:
                    result_lines.append(
                        f"    - {req['request_id']} at {req['timestamp']}"
                    )
                if len(reqs) > 3:
                    result_lines.append(f"    ... 외 {len(reqs) - 3}개")
        else:
            result_lines.append("미완료 요청이 없습니다.")
            result_lines.append("")
            result_lines.append("가능한 원인:")
            result_lines.append("  - 모든 요청이 OOM 전에 정상 완료됨")
            result_lines.append("  - 많은 요청에 걸쳐 메모리가 누적되었을 수 있음")
            result_lines.append("  - 요청 패턴과 응답 크기 분석 필요")

        return "\n".join(result_lines)

    except Exception as e:
        return f"로그 분석 중 오류 발생: {str(e)}"


@tool
def query_alb_access_logs(
    path: Annotated[
        str,
        "분석할 요청 경로 (예: /api/v1/heavy_endpoint)",
    ],
    oom_time: Annotated[
        str,
        "OOM 발생 시간 (ISO 형식, 예: 2025-01-15T14:30:00)",
    ],
    minutes_before: Annotated[
        int,
        "OOM 발생 전 몇 분간 분석할지 (기본값: 10)",
    ] = 10,
) -> str:
    """
    Athena를 통해 ALB 액세스 로그를 조회하여 요청/응답 페이로드 크기를 분석합니다.

    find_incomplete_requests에서 의심스러운 경로를 발견하면 이 도구로 페이로드 크기를 확인하세요.
    큰 응답(10KB+)이 많으면 메모리 누적 원인일 수 있습니다.

    반환값: 페이로드 크기 통계 (최대, 평균, 상위 5개 요청)
    """
    from api import athena

    # 시간 범위 계산
    oom_dt = datetime.fromisoformat(oom_time)
    start_dt = oom_dt - timedelta(minutes=minutes_before)
    end_dt = oom_dt + timedelta(minutes=1)  # OOM 후 1분 버퍼

    # 파티션 조건 생성 (ALB 로그는 YYYY/MM/DD로 파티셔닝)
    days = set()
    current = start_dt.date()
    while current <= end_dt.date():
        days.add(current.strftime("%Y/%m/%d"))
        current = current + timedelta(days=1)

    day_conditions = " OR ".join([f"day = '{day}'" for day in sorted(days)])

    # Athena 쿼리 생성
    query = f"""
    SELECT
        time,
        request_url,
        received_bytes,
        sent_bytes,
        target_status_code,
        target_processing_time,
        client_ip
    FROM ped_alb_access_logs_prd
    WHERE ({day_conditions})
        AND time >= '{start_dt.isoformat()}'
        AND time <= '{end_dt.isoformat()}'
        AND request_url LIKE '%{path}%'
    ORDER BY CAST(sent_bytes AS INTEGER) DESC
    LIMIT 50
    """

    try:
        results = athena.execute_and_wait(query, database="default")

        if "ResultSet" not in results:
            return f"'{path}' 경로에 대한 ALB 로그를 찾을 수 없습니다."

        rows = results["ResultSet"].get("Rows", [])
        if len(rows) <= 1:
            return f"'{path}' 경로에 대한 ALB 로그를 찾을 수 없습니다."

        # 첫 번째 행은 헤더
        data_rows = rows[1:]

        # 결과 파싱
        received_sizes = []
        sent_sizes = []
        parsed_results = []

        for row in data_rows:
            values = [col.get("VarCharValue", "") for col in row["Data"]]
            if len(values) >= 7:
                received = values[2]
                sent = values[3]
                if received.isdigit():
                    received_sizes.append(int(received))
                if sent.isdigit():
                    sent_sizes.append(int(sent))
                parsed_results.append(
                    {
                        "time": values[0],
                        "url": values[1],
                        "received_bytes": received,
                        "sent_bytes": sent,
                        "status": values[4],
                        "processing_time": values[5],
                        "client_ip": values[6],
                    }
                )

        # 결과 포맷팅
        result_lines = [
            "=" * 60,
            f"ALB 액세스 로그 분석: {path}",
            "=" * 60,
            f"분석 구간: {start_dt.isoformat()} ~ {end_dt.isoformat()}",
            f"총 요청 수: {len(parsed_results)}",
            "",
        ]

        if received_sizes:
            result_lines.append("요청 페이로드 (received_bytes):")
            result_lines.append(f"  최대: {max(received_sizes):,} bytes")
            result_lines.append(
                f"  평균: {sum(received_sizes) // len(received_sizes):,} bytes"
            )

        if sent_sizes:
            result_lines.append("응답 페이로드 (sent_bytes):")
            result_lines.append(f"  최대: {max(sent_sizes):,} bytes")
            result_lines.append(f"  평균: {sum(sent_sizes) // len(sent_sizes):,} bytes")

        result_lines.append("")
        result_lines.append("상위 5개 응답 (크기순):")
        for r in parsed_results[:5]:
            result_lines.append(
                f"  {r['time']}: sent={r['sent_bytes']:>10} bytes, "
                f"received={r['received_bytes']:>10} bytes, status={r['status']}"
            )

        return "\n".join(result_lines)

    except Exception as e:
        return f"ALB 로그 조회 중 오류 발생: {str(e)}"
