import os
import argparse
from datetime import datetime, timezone, timedelta
from typing import Any
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from github import Github
from github.PullRequest import PullRequest
from slack_sdk import WebClient
import tabulate

# wide chars 모드 활성화 (한글 폭 계산에 wcwidth 사용)
tabulate.WIDE_CHARS_MODE = True

# 환경 변수 로드
load_dotenv()

# 기본 설정
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = "C086HAVUFR8"  # 리뷰 통계를 보낼 채널 ID
ORG_NAME = "team-monolith-product"  # GitHub 조직 이름
DAYS = 7  # 조회할 데이터 기간 (일)


def fetch_pull_requests(
    github_client: Github, repo_owner: str, repo_name: str, days: int
) -> list[PullRequest]:
    """
    주어진 기간 동안의 PR을 가져옵니다.
    """
    # 날짜 계산
    since_date = datetime.now(timezone.utc) - timedelta(days=days)

    # 저장소 접근
    repo = github_client.get_repo(f"{repo_owner}/{repo_name}")

    # PR 조회: 모든 상태의 PR을 일괄로 가져옴
    all_pulls = []

    # 제한 없이 모든 기간 내 PR을 가져옴 (성능 최적화로 인해 제한 제거)
    MAX_PRS_PER_REPO = 100  # 충분히 높은 값으로 설정
    pr_count = 0

    # 모든 PR을 업데이트 날짜 기준 내림차순으로 가져옴 (가장 최근 항목부터)
    # state="all"로 open과 closed PR을 한 번에 가져옴
    all_prs_iterator = repo.get_pulls(state="all", sort="updated", direction="desc")

    # 필요한 만큼만 가져오기 - 페이지네이션 최소화
    for pr in all_prs_iterator:
        # 날짜가 범위를 벗어나면 중단 (업데이트 순으로 정렬되어 있으므로 최적화 가능)
        if pr.updated_at < since_date and pr.created_at < since_date:
            break

        # 클로즈된 PR의 경우 머지된 것만 포함
        if pr.state == "closed" and pr.merged_at is None:
            continue

        # PR을 결과 목록에 추가
        all_pulls.append(pr)
        pr_count += 1

        # 최대 개수에 도달하면 중단
        if pr_count >= MAX_PRS_PER_REPO:
            break

    return all_pulls


def get_pr_timeline_events(pr: PullRequest) -> list:
    """
    PR의 타임라인 이벤트를 가져옵니다.

    PR 객체에 타임라인 이벤트가 캐싱되어 있으면 추가 API 호출 없이 반환합니다.
    그렇지 않으면 GitHub API를 호출하여 타임라인 이벤트를 가져옵니다.

    Args:
        pr: 풀 리퀘스트 객체

    Returns:
        타임라인 이벤트 목록
    """
    # PR에 timeline_events 속성이 이미 있는지 확인 (캐싱)
    if hasattr(pr, "_timeline_events"):
        return pr._timeline_events

    # PR을 Issue로 변환하여 타임라인에 접근
    issue = pr.as_issue()
    timeline = issue.get_timeline()

    # 모든 타임라인 이벤트 수집
    events = []

    for event in timeline:
        # 이벤트 속성 확인
        event_type = event.event
        event_time = event.created_at

        # 리뷰 요청/제거 이벤트
        if event_type in ("review_requested", "review_request_removed"):
            if "requested_reviewer" not in event.raw_data:
                # Team 이 요청되는 경우 requested_team 만 존재
                continue

            reviewer = event.raw_data["requested_reviewer"]["login"]
            events.append(
                {
                    "type": event_type,
                    "time": event_time,
                    "reviewer": reviewer,
                }
            )

        elif event_type in ("reviewed"):
            # reviewed 이벤트는 다른 이벤트와 규격이 다릅니다.
            # actor 대신 user를 쓰고, created_at 대신 submitted_at을 사용합니다.
            reviewer = event.raw_data["user"]["login"]
            event_time = datetime.strptime(
                event.raw_data["submitted_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)

            events.append(
                {
                    "type": event_type,
                    "time": event_time,
                    "reviewer": reviewer,
                }
            )

        # Ready for review 이벤트
        elif event_type == "ready_for_review":
            events.append(
                {
                    "type": "ready_for_review",
                    "time": event_time,
                }
            )

    # 시간순 정렬
    events.sort(key=lambda e: e["time"])

    # 캐싱
    pr._timeline_events = events
    return events


def calculate_review_response_times(pr: PullRequest) -> dict[str, list[float]]:
    """
    PR의 타임라인 이벤트를 분석하여 리뷰어별 응답 시간을 계산합니다.

    Args:
        pr: 풀 리퀘스트 객체
        debug: 디버그 메시지 출력 여부

    Returns:
        리뷰어별 응답 시간 정보 딕셔너리
    """

    # 타임라인 이벤트 가져오기
    events = get_pr_timeline_events(pr)

    # 리뷰어별 상태 추적
    reviewer_status = {}  # 리뷰어 -> 상태 ('미요청', '요청됨', '응답함')
    reviewer_request_time = {}  # 리뷰어 -> 가장 최근 요청 시간

    # 결과 저장용
    response_times = {}  # 리뷰어 -> [응답 시간 목록]

    # 이벤트 처리
    for event in events:
        event_type = event["type"]
        event_time = event["time"]

        # 리뷰 요청 이벤트
        if event_type == "review_requested":
            reviewer = event["reviewer"]
            if reviewer:  # 유효한 리뷰어 확인
                # 요청 상태를 요청됨으로 설정하고 요청 시간 업데이트
                reviewer_status[reviewer] = "요청됨"
                reviewer_request_time[reviewer] = event_time

        # 리뷰 요청 제거 이벤트
        elif event_type == "review_request_removed":
            reviewer = event["reviewer"]
            if reviewer:  # 유효한 리뷰어 확인
                reviewer_status[reviewer] = "미요청"

                if reviewer in reviewer_request_time:
                    del reviewer_request_time[reviewer]

        # 리뷰 제출 이벤트
        elif event_type == "reviewed":
            reviewer = event["reviewer"]

            if not reviewer:  # 유효하지 않은 리뷰어 건너뛰기
                continue

            # 자기 PR에 자신이 리뷰한 경우 제외
            if pr.user and reviewer == pr.user.login:
                continue

            # 리뷰어가 요청 상태인 경우
            if reviewer_status.get(reviewer) == "요청됨":
                request_time = reviewer_request_time[reviewer]
                response_time = (
                    event_time - request_time
                ).total_seconds() / 3600  # 시간 단위

                # 응답 시간 기록
                if reviewer not in response_times:
                    response_times[reviewer] = []
                response_times[reviewer].append(response_time)

                # 상태 업데이트 (다음 요청 준비)
                reviewer_status[reviewer] = "응답함"
                if reviewer in reviewer_request_time:
                    del reviewer_request_time[reviewer]

            # 리뷰어가 요청 상태가 아닌 경우 (비요청 리뷰)
            elif reviewer not in reviewer_status or reviewer_status[reviewer] != "요청됨":
                # 비요청 리뷰는 통계에 포함하지 않는다.
                continue

    # PR이 병합됐을 때 리뷰가 요청된 상태인 경우 처리
    if pr.merged_at:
        for reviewer, status in reviewer_status.items():
            if status == "요청됨" and reviewer in reviewer_request_time:
                # 리뷰 요청 시간부터 PR 병합 시간까지의 시간 계산
                request_time = reviewer_request_time[reviewer]
                response_time = (
                    pr.merged_at - request_time
                ).total_seconds() / 3600  # 시간 단위

                # 응답 시간 기록
                if reviewer not in response_times:
                    response_times[reviewer] = []
                response_times[reviewer].append(response_time)

    # 최종 응답 시간 결과
    return response_times


def process_pr_reviews(pr: PullRequest) -> dict:
    """
    단일 PR의 리뷰를 병렬로 처리하기 위한 함수입니다.

    시계열 기반 접근 방식으로 리뷰어별 리뷰 요청-응답 시간을 계산합니다.

    Args:
        pr: 풀 리퀘스트 객체

    Returns:
        dict: 리뷰어별 통계
    """
    author = pr.user.login
    local_reviewer_stats = {}

    # 시계열 기반 리뷰 요청-응답 시간 계산
    reviewer_response_times = calculate_review_response_times(pr)

    # 리뷰어별 통계 구성
    for reviewer, response_times in reviewer_response_times.items():
        # 자신의 PR에 자신이 리뷰한 경우 제외 (이미 calculate_review_response_times에서 필터링됨)
        if reviewer == author:
            continue

        # 리뷰어 통계 초기화
        if reviewer not in local_reviewer_stats:
            local_reviewer_stats[reviewer] = {
                "review_count": 0,
                "response_times": [],
                "avg_response_time": 0,
                "prs_reviewed": set(),
                "overdue_count": 0,  # 24시간 초과 리뷰 수
            }

        # 리뷰 수 증가
        local_reviewer_stats[reviewer]["review_count"] += len(response_times)
        local_reviewer_stats[reviewer]["prs_reviewed"].add(pr.number)

        # 응답 시간 목록 추가
        local_reviewer_stats[reviewer]["response_times"].extend(response_times)

        # 24시간 초과 리뷰 수 계산
        for response_time in response_times:
            if response_time > 24:
                local_reviewer_stats[reviewer]["overdue_count"] += 1

    return local_reviewer_stats


def calculate_weekly_stats(
    pull_requests: list[PullRequest],
) -> dict[str, dict[str, Any]]:
    """
    주간 PR 리뷰 통계를 계산합니다.
    - 사용자별 리뷰 수
    - 평균 응답 시간
    - 24시간 초과 리뷰 비율
    """
    # 리뷰어 통계
    reviewer_stats = {}

    # 각 PR의 리뷰 데이터 처리
    for pr in pull_requests:
        local_reviewer_stats = process_pr_reviews(pr)

        # 리뷰어별 통계 결과 병합
        for reviewer, stats in local_reviewer_stats.items():
            if reviewer not in reviewer_stats:
                reviewer_stats[reviewer] = {
                    "review_count": 0,
                    "response_times": [],
                    "avg_response_time": 0,
                    "prs_reviewed": set(),
                    "overdue_count": 0,
                }

            # 통계 병합
            reviewer_stats[reviewer]["review_count"] += stats["review_count"]
            reviewer_stats[reviewer]["response_times"].extend(stats["response_times"])
            reviewer_stats[reviewer]["prs_reviewed"].update(stats["prs_reviewed"])
            reviewer_stats[reviewer]["overdue_count"] += stats["overdue_count"]

    # 평균 응답 시간 및 초과 비율 계산
    for reviewer, data in reviewer_stats.items():
        response_times = data.get("response_times", [])
        if response_times:
            data["avg_response_time"] = sum(response_times) / len(response_times)
            data["overdue_percentage"] = (
                data["overdue_count"] / len(response_times)
            ) * 100

        else:
            data["avg_response_time"] = 0
            data["overdue_percentage"] = 0

        # set을 길이로 변환 (JSON 직렬화를 위해)
        data["prs_reviewed"] = len(data["prs_reviewed"])

    return reviewer_stats


def calculate_daily_stats(pull_requests: list[PullRequest]) -> dict:
    """
    어제 발생한 리뷰에 대한 개발자별 응답 시간 통계를 계산합니다.

    Args:
        pull_requests: 전체 PR 목록

    Returns:
        개발자별 응답 시간 통계
    """
    # 어제 날짜 계산
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    yesterday_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)

    # 어제 리뷰된 PR만 필터링
    filtered_prs = []
    for pr in pull_requests:
        # 타임라인 이벤트 가져오기
        events = get_pr_timeline_events(pr)

        # 어제 발생한 리뷰 이벤트가 있는지 확인
        has_yesterday_review = any(
            event["type"] == "reviewed"
            and yesterday_start <= event["time"] <= yesterday_end
            for event in events
        )

        if has_yesterday_review:
            filtered_prs.append(pr)

    # 선별된 PR에 대한 리뷰 응답 시간 계산
    reviewer_data = {}
    for pr in filtered_prs:
        response_times = calculate_review_response_times(pr)

        # 저장소 이름 추출
        repo_name = pr.base.repo.full_name

        for reviewer, times in response_times.items():
            if reviewer not in reviewer_data:
                reviewer_data[reviewer] = []

            # 리뷰 시간과 PR 정보 저장
            for time in times:
                reviewer_data[reviewer].append(
                    {"repo": repo_name, "pr_number": pr.number, "response_time": time}
                )

    return reviewer_data


def format_reviewer_table(reviewer_stats: dict[str, dict[str, Any]]) -> str:
    """
    리뷰어 통계를 표 형식으로 포맷팅합니다.
    """
    table_data = []

    for reviewer, data in reviewer_stats.items():
        avg_time = data.get("avg_response_time", 0)
        overdue_percentage = data.get("overdue_percentage", 0)
        review_count = data.get("review_count", 0)

        # 24시간 초과 비율에 따른 표시
        status = "✅"
        if overdue_percentage > 50:
            status = "❌"
        elif overdue_percentage > 25:
            status = "⚠️"

        # 테이블 데이터 추가
        table_data.append(
            [
                reviewer,
                f"{avg_time:.1f}h",
                f"{overdue_percentage:.1f}%",
                review_count,
                status,
            ]
        )

    # 평균 응답 시간 기준으로 정렬
    table_data.sort(key=lambda x: float(x[1].replace("h", "")))

    # 표 헤더
    headers = ["리뷰어", "평균응답", "24h초과", "완료", "상태"]

    # 표 생성
    return tabulate.tabulate(table_data, headers=headers, tablefmt="simple")


def send_to_slack(
    slack_client: WebClient,
    channel_id: str,
    reviewer_stats: dict[str, dict[str, Any]],
    repo_stats: dict[str, int],
    days: int,
) -> dict:
    """
    통계 결과를 Slack에 전송합니다.

    Args:
        slack_client: Slack API 클라이언트
        channel_id: 슬랙 채널 ID
        reviewer_stats: 리뷰어 통계
        repo_stats: 저장소별 PR 수
        days: 데이터 기간 (일)

    Returns:
        전송된 메시지의 응답 정보
    """

    # 리뷰어 통계 표 생성
    reviewer_table = format_reviewer_table(reviewer_stats)

    # 메시지 작성
    title = "📊 코드 리뷰 통계 보고서"
    subtitle = f"지난 {days}일간 리뷰 활동 (기준: {datetime.now().strftime('%Y-%m-%d')})"

    # 코드 블록으로 표 감싸기
    code_block = f"```\n{reviewer_table}\n```"

    # 저장소별 통계
    repo_block = ""
    if repo_stats:
        repo_list = "\n".join(
            [
                f"• *{repo}*: {count}개 PR"
                for repo, count in repo_stats.items()
                if count > 0
            ]
        )
        if repo_list:
            repo_block = f"*분석된 저장소:*\n{repo_list}"

    # 추가 설명
    explanation = "• *평균응답*: 리뷰 요청부터 응답까지 평균 소요 시간\n• *24h초과*: 24시간 이상 소요된 리뷰 비율\n• *완료*: 완료한 리뷰 수"

    # 슬랙 메시지 블록 구성
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title, "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": subtitle}},
        {"type": "section", "text": {"type": "mrkdwn", "text": code_block}},
        {"type": "section", "text": {"type": "mrkdwn", "text": explanation}},
    ]

    # 저장소 통계가 있으면 추가
    if repo_block:
        blocks.append({"type": "divider"})
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": repo_block}}
        )

    # 슬랙 메시지 전송
    return slack_client.chat_postMessage(
        channel=channel_id,
        text=title,
        blocks=blocks,
    )


def format_daily_review_message(reviewer_data: dict) -> str:
    """
    일간 리뷰 피드백 메시지를 슬랙에 보기 좋게 포맷팅합니다.

    Args:
        reviewer_data: 개발자별 리뷰 응답 시간 데이터

    Returns:
        포맷팅된 메시지
    """
    if not reviewer_data:
        return "*어제 발생한 리뷰가 없습니다.*"

    message_parts = ["*어제의 리뷰 응답 시간 (개발자별)*"]

    # 응답 시간에 따른 아이콘 표시
    def get_time_emoji(time: float) -> str:
        if time < 1:
            return ":zap:"  # 번개 (1시간 미만: 매우 빠름)
        elif time < 4:
            return ":white_check_mark:"  # 체크마크 (4시간 미만: 양호)
        elif time < 8:
            return ":hourglass_flowing_sand:"  # 모래시계 (8시간 미만: 보통)
        elif time < 24:
            return ":turtle:"  # 거북이 (24시간 미만: 느림)
        else:
            return ":snail:"  # 달팽이 (24시간 이상: 매우 느림)

    # 리뷰어별로 정렬 (알파벳 순)
    for reviewer in sorted(reviewer_data.keys()):
        reviews = reviewer_data[reviewer]
        # 리뷰 시간별로 정렬 (빠른 응답 시간 순)
        sorted_reviews = sorted(reviews, key=lambda x: x["response_time"])

        reviewer_section = [f"*{reviewer}* 님"]

        for review in sorted_reviews:
            repo = review["repo"].split("/")[1]  # 조직명 제외하고 저장소명만 추출
            pr_number = review["pr_number"]
            response_time = review["response_time"]

            # 시간 포맷팅 (소수점 첫째 자리까지)
            formatted_time = f"{response_time:.1f}"

            # 응답 시간에 따른 아이콘
            time_emoji = get_time_emoji(response_time)

            # PR 링크 생성
            pr_link = f"<https://github.com/team-monolith-product/{repo}/pull/{pr_number}|{repo}#{pr_number}>"

            reviewer_section.append(f"{time_emoji} {pr_link}: *{formatted_time}* 시간")

        message_parts.append("\n".join(reviewer_section))

    return "\n\n".join(message_parts)


def send_daily_review_feedback(
    slack_client: WebClient, thread_ts: str, message: str
) -> None:
    """
    일간 리뷰 피드백을 주간 통계 스레드에 전송합니다.
    각 개발자마다 별도의 메시지로 전송합니다.

    Args:
        slack_client: Slack API 클라이언트
        thread_ts: 스레드 타임스탬프
        message: 전송할 메시지
    """
    # 메시지 분할 (헤더 부분과 각 개발자별 섹션으로 분리)
    message_parts = message.split("\n\n")
    header = message_parts[0]  # 첫 번째 부분은 헤더
    developer_sections = message_parts[1:]  # 나머지는 개발자별 섹션

    # 헤더 메시지 전송
    slack_client.chat_postMessage(
        channel=SLACK_CHANNEL_ID,
        text="어제의 리뷰 응답 시간",
        thread_ts=thread_ts,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": header}}],
    )

    # 각 개발자별로 별도의 메시지 전송
    for section in developer_sections:
        # 개발자별 섹션을 각각 전송
        slack_client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=section.split("\n")[0],  # 첫 줄(개발자 이름)을 fallback 텍스트로 사용
            thread_ts=thread_ts,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": section},
                }
            ],
        )


def get_active_repos(
    github_client: Github, org_name: str, min_activity_days: int = 30
) -> list:
    """
    주어진 조직에서 최근 활동이 있는 저장소 목록을 가져옵니다.

    Args:
        github_client: GitHub API 클라이언트
        org_name: 조직 이름
        min_activity_days: 최근 활동 기간 (일)

    Returns:
        활성 저장소 목록 (owner/name 형식)
    """
    # 최소 활동 기간 계산
    min_activity_date = datetime.now(timezone.utc) - timedelta(days=min_activity_days)

    # 조직의 모든 저장소 가져오기
    org = github_client.get_organization(org_name)
    all_repos = list(org.get_repos())  # 페이지네이션 완료를 위해 리스트로 변환

    # 최근 활동이 있는 저장소만 필터링
    active_repos = []

    for repo in all_repos:
        if repo.archived:
            continue
        # fork된 저장소는 제외
        if repo.fork:
            continue

        if not repo.private:
            continue

        # 최근 업데이트 확인
        if repo.updated_at >= min_activity_date or repo.pushed_at >= min_activity_date:
            active_repos.append(f"{org_name}/{repo.name}")

    return active_repos


def fetch_all_pr_data(
    github_client: Github, days: int
) -> tuple[list[PullRequest], dict[str, int]]:
    """
    모든 PR 데이터를 병렬로 한 번에 가져옵니다.
    각 PR에 대한 타임라인 이벤트도 함께 사전 로드합니다.

    Args:
        github_client: GitHub API 클라이언트
        days: 조회할 데이터 기간 (일)

    Returns:
        (모든 PR 목록, 저장소별 PR 수 통계)
    """
    # 조직의 활성 저장소 조회
    repositories = get_active_repos(github_client, ORG_NAME, days)

    # 병렬로 각 저장소의 PR을 가져옴
    all_pull_requests = []
    repo_stats = {}  # 저장소별 PR 수 추적

    # 저장소 PR 병렬 조회를 위한 함수
    def fetch_repo_prs(repo_full_name, days):
        repo_owner, repo_name = repo_full_name.split("/")
        repo_prs = fetch_pull_requests(github_client, repo_owner, repo_name, days)
        return repo_full_name, repo_prs

    # 저장소 병렬 처리를 위한 설정
    REPO_MAX_WORKERS = min(30, len(repositories))  # 저장소 수에 따라 동적으로 조정

    with ThreadPoolExecutor(max_workers=REPO_MAX_WORKERS) as executor:
        futures = [
            executor.submit(fetch_repo_prs, repo_full_name, days)
            for repo_full_name in repositories
        ]

        for future in concurrent.futures.as_completed(futures):
            repo_full_name, repo_prs = future.result()
            if repo_prs:  # 결과가 있는 경우만 추가
                all_pull_requests.extend(repo_prs)
                repo_stats[repo_full_name] = len(repo_prs)

    # 모든 PR의 타임라인 이벤트 사전 로드 (병렬 처리)
    with ThreadPoolExecutor(max_workers=min(50, len(all_pull_requests))) as executor:
        # 모든 PR에 대해 병렬로 타임라인 이벤트 로드
        list(executor.map(get_pr_timeline_events, all_pull_requests))

    return all_pull_requests, repo_stats


def main():
    """
    GitHub PR 리뷰 통계를 수집하고 Slack에 전송합니다.

    --dry-run 옵션이 주어지면 실제 메시지 전송 없이 콘솔에만 출력합니다.
    """
    parser = argparse.ArgumentParser(description="GitHub PR 리뷰 통계 수집")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="메시지를 Slack에 전송하지 않고 콘솔에만 출력합니다",
    )

    args = parser.parse_args()

    github_client = Github(GITHUB_TOKEN)
    slack_client = WebClient(token=SLACK_BOT_TOKEN)

    # 1. 모든 PR 데이터를, 타임라인 이벤트와 함께 한 번만 가져옵니다
    all_pull_requests, repo_stats = fetch_all_pr_data(github_client, DAYS)

    if not all_pull_requests:
        return

    # 2. 한 번 가져온 데이터를 사용하여 주간 통계와 일간 통계를 모두 계산합니다
    weekly_stats = calculate_weekly_stats(all_pull_requests)
    daily_stats = calculate_daily_stats(all_pull_requests)

    # 결과 포맷팅
    reviewer_table = format_reviewer_table(weekly_stats)
    daily_message = format_daily_review_message(daily_stats)

    repo_activity = "\n".join(
        [f"• {repo}: {count}개 PR" for repo, count in repo_stats.items() if count > 0]
    )

    if args.dry_run:
        print("=== DRY RUN MODE ===")
        print("코드 리뷰 통계 (리뷰어):")
        print(reviewer_table)
        print("\n저장소별 PR 수:")
        print(repo_activity)
        print("=====================")

        print("\n=== 일간 리뷰 피드백 ===")
        print(daily_message)
        print("=====================")
    else:
        # 주간 통계 메시지 전송
        response = send_to_slack(
            slack_client, SLACK_CHANNEL_ID, weekly_stats, repo_stats, DAYS
        )

        # 일간 리뷰 피드백을 주간 통계의 스레드로 추가
        thread_ts = response["ts"]
        send_daily_review_feedback(slack_client, thread_ts, daily_message)


if __name__ == "__main__":
    main()
