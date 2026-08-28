"""Rails OOM 분석에 필요한 CloudWatch 로그 파싱 함수입니다."""

import json
import re
import sys
from datetime import datetime
from typing import Any


def format_time(timestamp_ms: int | None) -> str:
    """밀리초 타임스탬프를 읽을 수 있는 로컬 시각으로 바꿉니다."""
    if not timestamp_ms:
        return "N/A"
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def list_streams(client: Any, log_group: str, pod_name: str) -> list[dict[str, Any]]:
    """CloudWatch에서 pod 이름과 일치하는 로그 스트림을 조회합니다."""
    streams: list[dict[str, Any]] = []

    try:
        paginator = client.get_paginator("describe_log_streams")
        for page in paginator.paginate(
            logGroupName=log_group, orderBy="LastEventTime", descending=True
        ):
            for stream in page["logStreams"]:
                stream_name = stream["logStreamName"]
                if pod_name not in stream_name:
                    continue
                streams.append(
                    {
                        "name": stream_name,
                        "first_event_time": stream.get("firstEventTimestamp"),
                        "last_event_time": stream.get("lastEventTimestamp"),
                    }
                )
        return streams
    except Exception as error:
        print(f"로그 스트림 조회 실패: {error}", file=sys.stderr)
        return []


def get_stream_last_timestamp(
    client: Any, log_group: str, stream_name: str
) -> int | None:
    """지정한 로그 스트림의 마지막 이벤트 타임스탬프를 읽습니다."""
    try:
        response = client.describe_log_streams(
            logGroupName=log_group,
            logStreamNamePrefix=stream_name,
            limit=1,
        )
        for stream in response.get("logStreams", []):
            if stream["logStreamName"] == stream_name:
                return stream.get("lastEventTimestamp")
        return None
    except Exception as error:
        print(f"로그 스트림 정보 조회 실패: {error}", file=sys.stderr)
        return None


def fetch_logs_from_cloudwatch(
    client: Any,
    log_group: str,
    log_stream: str,
    start_time: int | None = None,
    end_time: int | None = None,
) -> list[dict[str, Any]]:
    """CloudWatch 로그 스트림에서 지정한 시간 범위의 이벤트를 읽습니다."""
    kwargs: dict[str, Any] = {
        "logGroupName": log_group,
        "logStreamNames": [log_stream],
    }
    if start_time:
        kwargs["startTime"] = start_time
    if end_time:
        kwargs["endTime"] = end_time

    logs: list[dict[str, Any]] = []
    try:
        paginator = client.get_paginator("filter_log_events")
        for page in paginator.paginate(**kwargs):
            logs.extend(
                {
                    "message": event["message"],
                    "timestamp": event["timestamp"],
                }
                for event in page["events"]
            )
        return logs
    except Exception as error:
        print(f"CloudWatch 로그 조회 실패: {error}", file=sys.stderr)
        return []


def parse_log_message(line: str) -> dict[str, str | None] | None:
    """CloudWatch JSON 문자열에서 Rails 로그와 시각을 추출합니다."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    log_text = data.get("log", "")
    if not isinstance(log_text, str):
        return None
    timestamp_match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", log_text)
    log_match = re.search(r"INFO -- : (.*)", log_text)
    if not log_match:
        return None
    return {
        "log": log_match.group(1),
        "timestamp": timestamp_match.group(1) if timestamp_match else None,
    }


def extract_request_id(log_line: str) -> str | None:
    """Rails 로그에서 요청 ID를 추출합니다."""
    match = re.search(r"\[([a-f0-9\-]{36})\]", log_line)
    return match.group(1) if match else None


def is_started_request(log_line: str) -> bool:
    """Started 요청 로그인지 확인합니다."""
    return "Started " in log_line and any(
        method in log_line
        for method in [" GET ", " POST ", " PUT ", " PATCH ", " DELETE "]
    )


def is_completed_request(log_line: str) -> bool:
    """Completed 요청 로그인지 확인합니다."""
    return "Completed " in log_line and bool(re.search(r"Completed \d+", log_line))


def extract_request_info(log_line: str) -> dict[str, str] | None:
    """Started 로그에서 HTTP 메서드, 경로와 IP를 추출합니다."""
    match = re.search(
        r'Started (GET|POST|PUT|PATCH|DELETE) \\?"([^"\\]+)\\?" for ([\d.]+)',
        log_line,
    )
    if not match:
        return None
    return {
        "method": match.group(1),
        "path": match.group(2),
        "ip": match.group(3),
    }


def should_exclude_path(path: str) -> bool:
    """상태 확인용 경로를 OOM 의심 요청에서 제외합니다."""
    return path in {
        "/health_check",
        "/metrics",
        "/healthz",
        "/readiness",
        "/liveness",
    }
