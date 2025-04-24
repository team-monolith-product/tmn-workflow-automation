import os
import argparse
from datetime import datetime, timezone, timedelta
from typing import Any
from collections import defaultdict
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


def get_pr_reviews(pr: PullRequest) -> list[dict[str, Any]]:
    """
    PR의 리뷰를 가져오고, 사람이 작성한 리뷰만 필터링합니다.
    """
    reviews = pr.get_reviews()

    # 봇이 작성한 리뷰 제외
    filtered_reviews = []
    for review in reviews:
        if review.user and review.user.type != "Bot":
            filtered_reviews.append(
                {
                    "id": review.id,
                    "user": review.user.login,
                    "state": review.state,
                    "submitted_at": review.submitted_at,
                    "body": review.body,
                }
            )

    return filtered_reviews


def get_pr_timeline_events(pr: PullRequest) -> list:
    """
    PR의 타임라인 이벤트를 가져옵니다.

    Args:
        pr: 풀 리퀘스트 객체
        debug: 디버그 메시지 출력 여부

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

    # 타임라인 이벤트 목록

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
            ).replace(
                tzinfo=timezone.utc
            )

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

    # PR 생성 이벤트 추가 (항상 첫 번째)
    # TODO: 불필요한 코드 제거
    events.insert(
        0,
        {
            "type": "created",
            "time": pr.created_at,
            "author": pr.user.login if pr.user else "unknown",
        },
    )

    # 시간순 정렬
    events.sort(key=lambda e: e["time"])

    # 캐싱
    pr._timeline_events = events
    return events


def calculate_review_response_times(pr: PullRequest) -> dict:
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

    # PR이 Ready 상태가 된 시간 (기본값)
    ready_time = pr.created_at
    for event in events:
        if event["type"] == "ready_for_review":
            ready_time = event["time"]
            break

    # 리뷰어별 상태 추적
    reviewer_status = {}  # 리뷰어 -> 상태 ('미요청', '요청됨', '응답함')
    reviewer_request_time = {}  # 리뷰어 -> 가장 최근 요청 시간
    reviewer_last_request_time = (
        {}
    )  # 리뷰어 -> 마지막으로 요청된 시간 (요청 제거되어도 유지)

    # PR 메타데이터 저장 - 디버깅에 유용
    pr_metadata = {
        "number": pr.number,
        "title": pr.title,
        "url": pr.html_url,
        "created_at": pr.created_at,
        "updated_at": pr.updated_at,
        "merged_at": pr.merged_at if hasattr(pr, "merged") and pr.merged else None,
        "reviewer_requests": {},  # 리뷰어별 요청/제거 이벤트 기록
        "reviews": {},  # 리뷰어별 리뷰 제출 기록
    }

    # 결과 저장용
    response_times = {}  # 리뷰어 -> [응답 시간 목록]

    # 초기 리뷰어 설정 (PR 생성 시 지정된 리뷰어)
    requests = pr.get_review_requests()
    if requests and len(requests) > 0:
        initial_reviewers = [r.login for r in requests[0] if hasattr(r, "login")]
        for reviewer in initial_reviewers:
            reviewer_status[reviewer] = "요청됨"
            reviewer_request_time[reviewer] = ready_time  # Ready 시간부터 계산
            reviewer_last_request_time[reviewer] = ready_time  # 마지막 요청 시간 기록

    # 이벤트 처리
    for event in events:
        event_type = event["type"]
        event_time = event["time"]

        # 리뷰 요청 이벤트
        if event_type == "review_requested" and "reviewer" in event:
            reviewer = event["reviewer"]
            if reviewer:  # 유효한 리뷰어 확인
                # 요청 상태를 요청됨으로 설정하고 요청 시간 업데이트
                reviewer_status[reviewer] = "요청됨"
                reviewer_request_time[reviewer] = event_time
                reviewer_last_request_time[reviewer] = (
                    event_time  # 마지막 요청 시간 업데이트
                )

                # PR의 메타데이터 저장 (디버깅용)
                if reviewer not in pr_metadata["reviewer_requests"]:
                    pr_metadata["reviewer_requests"][reviewer] = []
                pr_metadata["reviewer_requests"][reviewer].append(
                    {
                        "action": "requested",
                        "time": str(event_time),
                    }
                )

        # 리뷰 요청 제거 이벤트
        elif event_type == "review_request_removed" and "reviewer" in event:
            reviewer = event["reviewer"]
            if reviewer:  # 유효한 리뷰어 확인
                old_status = reviewer_status.get(reviewer, "알 수 없음")
                reviewer_status[reviewer] = "미요청"

                # PR의 메타데이터 저장 (디버깅용)
                if reviewer not in pr_metadata["reviewer_requests"]:
                    pr_metadata["reviewer_requests"][reviewer] = []
                pr_metadata["reviewer_requests"][reviewer].append(
                    {
                        "action": "removed",
                        "time": str(event_time),
                        "previous_status": old_status,
                    }
                )

                if reviewer in reviewer_request_time:
                    del reviewer_request_time[reviewer]

        # 리뷰 제출 이벤트
        elif event_type == "reviewed" and "reviewer" in event:
            reviewer = event["reviewer"]

            if not reviewer:  # 유효하지 않은 리뷰어 건너뛰기
                continue

            # 자기 PR에 자신이 리뷰한 경우 제외
            if pr.user and reviewer == pr.user.login:
                continue

            # PR 메타데이터에 리뷰 정보 저장 (디버깅용)
            if reviewer not in pr_metadata["reviews"]:
                pr_metadata["reviews"][reviewer] = []
            pr_metadata["reviews"][reviewer].append(
                {
                    "time": str(event_time),
                    "status": reviewer_status.get(reviewer, "알 수 없음"),
                    "last_request": (
                        str(reviewer_last_request_time.get(reviewer, None))
                        if reviewer in reviewer_last_request_time
                        else None
                    ),
                    "current_request": (
                        str(reviewer_request_time.get(reviewer, None))
                        if reviewer in reviewer_request_time
                        else None
                    ),
                }
            )

            # 리뷰어가 요청 상태인 경우
            if (
                reviewer_status.get(reviewer) == "요청됨"
                and reviewer in reviewer_request_time
            ):
                request_time = reviewer_request_time[reviewer]
                time_diff = (
                    event_time - request_time
                ).total_seconds() / 3600  # 시간 단위

                # 실제 계산된 시간 그대로 사용
                response_time = time_diff

                # 응답 시간 기록
                if reviewer not in response_times:
                    response_times[reviewer] = []
                response_times[reviewer].append(response_time)

                # 상태 업데이트 (다음 요청 준비)
                reviewer_status[reviewer] = "응답함"
                if reviewer in reviewer_request_time:
                    del reviewer_request_time[reviewer]

            # 리뷰어가 요청 상태가 아닌 경우 (비요청 리뷰)
            elif (
                reviewer not in reviewer_status or reviewer_status[reviewer] != "요청됨"
            ):
                # 비요청 리뷰는 통계에 포함하지 않는다.
                continue
                # # 이전에 요청된 적이 있는지 확인
                # if reviewer in reviewer_last_request_time:
                #     # 마지막 요청 시간부터 계산 (리뷰 요청이 제거된 경우)
                #     request_time = reviewer_last_request_time[reviewer]
                # else:
                #     # 한 번도 요청된 적이 없는 경우 Ready 시간부터 계산
                #     request_time = ready_time

                # # 응답 시간 계산
                # time_diff = (
                #     event_time - request_time
                # ).total_seconds() / 3600  # 시간 단위

                # # Ready 시간보다 빠른 이벤트 발생(마이너스 시간)은 데이터 불일치 문제일 수 있음

                # # 모든 경우에 실제 계산된 시간 그대로 사용
                # response_time = time_diff

                # # 응답 시간 기록
                # if reviewer not in response_times:
                #     response_times[reviewer] = []
                # response_times[reviewer].append(response_time)

    # 최종 응답 시간 결과

    return response_times


def process_pr_reviews(pr: PullRequest) -> tuple[dict, PullRequest, dict, int, bool]:
    """
    단일 PR의 리뷰를 병렬로 처리하기 위한 함수입니다.

    시계열 기반 접근 방식으로 리뷰어별 리뷰 요청-응답 시간을 계산합니다.

    Args:
        pr: 풀 리퀘스트 객체
        author_stats: 저자 통계 정보

    Returns:
        tuple[dict, PullRequest, dict, int, bool]:
            (리뷰어별 통계, PR, 요청된 리뷰어 정보, 리뷰 수, 리뷰 존재 여부)
    """
    if not pr.user:
        return {}, None, {}, 0, False

    author = pr.user.login
    local_reviewer_stats = {}
    open_pr = None
    local_reviewer_to_requested_prs = {}

    # 열린 PR인 경우
    if pr.state == "open":
        open_pr = pr

        # 현재 요청된 리뷰어 찾기
        requested_reviewers = pr.get_review_requests()
        if requested_reviewers and len(requested_reviewers) > 0:
            for user in requested_reviewers[0]:  # [0]은 사용자 목록, [1]은 팀 목록
                if hasattr(user, "login"):
                    local_reviewer_to_requested_prs[user.login] = pr.number

    # 리뷰 정보 수집
    reviews = get_pr_reviews(pr)
    has_reviews = len(reviews) > 0

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
                "pending_reviews": 0,  # 대기 중인 리뷰 요청 수
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

    return (
        local_reviewer_stats,
        open_pr,
        local_reviewer_to_requested_prs,
        len(reviews),
        has_reviews,
    )


def calculate_stats(pull_requests: list[PullRequest]) -> dict[str, dict[str, Any]]:
    """
    PR 리뷰 통계를 계산합니다.
    - 사용자별 리뷰 수
    - 평균 응답 시간
    - 24시간 초과 리뷰 비율

    ThreadPoolExecutor를 사용하여 PR 리뷰 데이터 수집을 병렬화합니다.
    """
    # 리뷰어 통계
    reviewer_stats = {}

    # PR 작성자 통계 (받은 리뷰 수, 대기 중인 PR 등)
    author_stats = {}

    # 리뷰 요청된 PR 목록 (reviewer -> PR set)
    reviewer_to_requested_prs = defaultdict(set)

    # 열린 PR 목록을 추적
    open_prs = []

    # 초기화 - 모든 PR 작성자의 통계
    for pr in pull_requests:
        if pr.user:
            author = pr.user.login
            if author not in author_stats:
                author_stats[author] = {
                    "total_prs": 0,
                    "open_prs": 0,
                    "reviewed_prs": 0,
                    "waiting_for_review": 0,
                }
            author_stats[author]["total_prs"] += 1

    reviews_count = 0

    # 병렬 실행을 위한 설정 - 더 많은 동시 요청으로 성능 향상
    MAX_WORKERS = min(
        50, len(pull_requests)
    )  # GitHub 2차 레이트 제한 고려하면서 충분히 높게 설정

    # ThreadPoolExecutor를 사용한 병렬 처리
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 모든 PR에 대해 병렬로 리뷰 데이터 수집 작업 시작
        futures = {
            executor.submit(process_pr_reviews, pr): (
                pr_index,
                pr,
            )
            for pr_index, pr in enumerate(pull_requests, 1)
        }

        for future in concurrent.futures.as_completed(futures):
            pr_index, pr = futures[future]
            # 결과 처리
            (
                local_reviewer_stats,
                open_pr,
                local_requested_reviewers,
                review_count,
                has_reviews,
            ) = future.result()

            reviews_count += review_count

            # 열린 PR 추가
            if open_pr:
                open_prs.append(open_pr)
                author = open_pr.user.login
                author_stats[author]["open_prs"] += 1

            # 리뷰어별 요청 PR 추가
            for reviewer, pr_number in local_requested_reviewers.items():
                reviewer_to_requested_prs[reviewer].add(pr_number)

            # 리뷰어별 통계 결과 병합
            for reviewer, stats in local_reviewer_stats.items():
                if reviewer not in reviewer_stats:
                    reviewer_stats[reviewer] = {
                        "review_count": 0,
                        "response_times": [],
                        "avg_response_time": 0,
                        "prs_reviewed": set(),
                        "overdue_count": 0,
                        "pending_reviews": 0,
                    }

                # 통계 병합
                reviewer_stats[reviewer]["review_count"] += stats["review_count"]
                reviewer_stats[reviewer]["response_times"].extend(
                    stats["response_times"]
                )
                reviewer_stats[reviewer]["prs_reviewed"].update(stats["prs_reviewed"])
                reviewer_stats[reviewer]["overdue_count"] += stats["overdue_count"]

            # 작성자 통계에 리뷰 받은 PR 수 업데이트
            if has_reviews and pr.user:
                author = pr.user.login
                author_stats[author]["reviewed_prs"] += 1

    # 대기 중인 리뷰 요청 수 업데이트
    for reviewer, pr_numbers in reviewer_to_requested_prs.items():
        if reviewer in reviewer_stats:
            reviewer_stats[reviewer]["pending_reviews"] = len(pr_numbers)
        else:
            reviewer_stats[reviewer] = {
                "review_count": 0,
                "avg_response_time": 0,
                "prs_reviewed": set(),
                "overdue_count": 0,
                "pending_reviews": len(pr_numbers),
            }

    # 대기 중인 PR 수 업데이트
    for author in author_stats:
        waiting_count = 0
        for pr in open_prs:
            if pr.user and pr.user.login == author:
                waiting_count += 1
        author_stats[author]["waiting_for_review"] = waiting_count

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

        # details 필드 제거 (JSON 직렬화를 위해)
        if "response_times_details" in data:
            del data["response_times_details"]

    return {"reviewers": reviewer_stats, "authors": author_stats}


def format_reviewer_table(reviewer_stats: dict[str, dict[str, Any]]) -> str:
    """
    리뷰어 통계를 표 형식으로 포맷팅합니다.
    """
    table_data = []

    for reviewer, data in reviewer_stats.items():
        avg_time = data.get("avg_response_time", 0)
        overdue_percentage = data.get("overdue_percentage", 0)
        review_count = data.get("review_count", 0)
        pending_reviews = data.get("pending_reviews", 0)

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
                pending_reviews,
                status,
            ]
        )

    # 평균 응답 시간 기준으로 정렬
    table_data.sort(key=lambda x: float(x[1].replace("h", "")))

    # 표 헤더
    headers = ["리뷰어", "평균응답", "24h초과", "완료", "대기", "상태"]

    # 표 생성
    return tabulate.tabulate(table_data, headers=headers, tablefmt="simple")


def send_to_slack(
    slack_client: WebClient,
    channel_id: str,
    stats: dict[str, dict[str, Any]],
    days: int,
) -> None:
    """
    통계 결과를 Slack에 전송합니다.
    """
    reviewer_stats = stats.get("reviewers", {})
    repo_stats = stats.get("repo_stats", {})

    # 리뷰어 통계 표 생성
    reviewer_table = format_reviewer_table(reviewer_stats)

    # 메시지 작성
    title = "📊 코드 리뷰 통계 보고서"
    subtitle = (
        f"지난 {days}일간 리뷰 활동 (기준: {datetime.now().strftime('%Y-%m-%d')})"
    )

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
    explanation = "• *평균응답*: 리뷰 요청부터 응답까지 평균 소요 시간\n• *24h초과*: 24시간 이상 소요된 리뷰 비율\n• *완료*: 완료한 리뷰 수\n• *대기*: 리뷰 요청 받았으나 아직 응답하지 않은 수"

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
    slack_client.chat_postMessage(
        channel=channel_id,
        text=title,
        blocks=blocks,
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

        # 보관처리된 저장소는 제외
        if repo.archived:
            continue

        # fork된 저장소는 제외
        if repo.fork:
            continue

        # private 저장소만 포함 (선택적)
        if not repo.private:
            continue

        # 최근 업데이트 확인
        if repo.updated_at >= min_activity_date or repo.pushed_at >= min_activity_date:
            active_repos.append(f"{org_name}/{repo.name}")

    return active_repos


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

    # GitHub API 초기화
    github_client = Github(GITHUB_TOKEN)

    # Slack API 초기화
    slack_client = WebClient(token=SLACK_BOT_TOKEN)

    # 조직의 활성 저장소 조회
    repositories = get_active_repos(github_client, ORG_NAME)

    # 병렬로 각 저장소의 PR을 가져옴
    all_pull_requests = []
    repo_stats = {}  # 저장소별 PR 수 추적

    # 저장소 PR 병렬 조회를 위한 함수
    def fetch_repo_prs(repo_index, repo_full_name, days):
        repo_owner, repo_name = repo_full_name.split("/")

        repo_prs = fetch_pull_requests(github_client, repo_owner, repo_name, days)
        return repo_full_name, repo_prs

    # 저장소 병렬 처리를 위한 설정
    REPO_MAX_WORKERS = min(30, len(repositories))  # 저장소 수에 따라 동적으로 조정

    # ThreadPoolExecutor를 사용한 병렬 처리
    with ThreadPoolExecutor(max_workers=REPO_MAX_WORKERS) as executor:
        # 모든 저장소에 대해 병렬로 PR 조회 시작
        futures = {
            executor.submit(
                fetch_repo_prs, repo_index, repo_full_name, DAYS
            ): repo_index
            for repo_index, repo_full_name in enumerate(repositories, 1)
        }

        # 결과 수집
        for future in concurrent.futures.as_completed(futures):
            repo_full_name, repo_prs = future.result()
            if repo_prs:  # 결과가 있는 경우만 추가
                all_pull_requests.extend(repo_prs)
                repo_stats[repo_full_name] = len(repo_prs)

    if not all_pull_requests:
        return

    # 통계 계산
    stats = calculate_stats(all_pull_requests)

    # 리뷰어 통계 표시
    reviewer_table = format_reviewer_table(stats["reviewers"])

    # 저장소별 통계
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
    else:
        # Slack에 전송
        # 저장소별 통계도 함께 전송
        stats["repo_stats"] = repo_stats
        send_to_slack(slack_client, SLACK_CHANNEL_ID, stats, DAYS)


if __name__ == "__main__":
    main()
