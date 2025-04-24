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


def get_pr_ready_time(pr: PullRequest) -> datetime:
    """
    PR이 Draft에서 Ready로 변경된 시간을 가져옵니다.
    모든 PR의 타임라인을 검사하여 Ready 이벤트를 찾습니다.
    Ready 이벤트가 없으면 PR 생성 시간을 반환합니다.
    
    Args:
        pr: 풀 리퀘스트 객체
        
    Returns:
        PR이 Ready로 변경된 시간 또는 PR 생성 시간
    """
    # 캐시 적중률 추적을 위한 전역 변수
    if not hasattr(get_pr_ready_time, 'ready_time_cache_hits'):
        get_pr_ready_time.ready_time_cache_hits = 0
        get_pr_ready_time.ready_time_cache_misses = 0
    
    # PR에 ready_time 속성이 이미 있는지 확인 (캐싱)
    if hasattr(pr, '_ready_time'):
        get_pr_ready_time.ready_time_cache_hits += 1
        return pr._ready_time
    
    get_pr_ready_time.ready_time_cache_misses += 1
    
    # PR을 Issue로 변환하여 타임라인에 접근
    try:
        issue = pr.as_issue()
        timeline = issue.get_timeline()
        
        # ready_for_review 이벤트 찾기
        for event in timeline:
            if hasattr(event, 'event') and event.event == 'ready_for_review':
                # 결과 캐싱
                pr._ready_time = event.created_at
                return event.created_at
        
        # 이벤트를 찾지 못했다면 PR 생성 시간 반환
        # (Draft로 생성된 적이 없거나, 타임라인에 이벤트가 없는 경우)
        pr._ready_time = pr.created_at
        return pr.created_at
        
    except Exception as e:
        pr._ready_time = pr.created_at
        return pr.created_at


def process_pr_reviews(
    pr: PullRequest, author_stats: dict
) -> tuple[dict, list, dict, int, bool]:
    """
    단일 PR의 리뷰를 병렬로 처리하기 위한 함수입니다.

    Returns:
        tuple[dict, list, dict, int, bool]:
            (리뷰어별 통계, PR 요청자 정보, 요청된 리뷰어 정보, 리뷰 수, 리뷰 존재 여부)
    """
    if not pr.user:
        return {}, [], {}, 0, False

    author = pr.user.login
    local_reviewer_stats = {}
    open_pr = None
    local_reviewer_to_requested_prs = {}

    # 열린 PR인 경우
    if pr.state == "open":
        open_pr = pr

        # 리뷰 요청자 찾기
        requested_reviewers = pr.get_review_requests()
        for user in requested_reviewers[0]:  # [0]은 사용자 목록, [1]은 팀 목록
            local_reviewer_to_requested_prs[user.login] = pr.number

    # 리뷰 정보 수집
    reviews = get_pr_reviews(pr)
    has_reviews = False

    for review in reviews:
        has_reviews = True
        reviewer = review.get("user")
        submitted_at = review.get("submitted_at")

        # 자신의 PR에 자신이 리뷰한 경우 제외
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
        local_reviewer_stats[reviewer]["review_count"] += 1
        local_reviewer_stats[reviewer]["prs_reviewed"].add(pr.number)

        # PR의 Ready 시간 조회 (Draft -> Ready 변경 시간 또는 PR 생성 시간)
        ready_time = get_pr_ready_time(pr)
        
        # 응답 시간 계산 (PR Ready 시간부터 리뷰 제출 시간까지)
        if ready_time and submitted_at:
            response_time = (
                submitted_at - ready_time
            ).total_seconds() / 3600  # 시간 단위
            local_reviewer_stats[reviewer]["response_times"].append(response_time)

            # 24시간 초과 여부 확인
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

    # 리뷰 데이터 수집 시간 측정
    reviews_fetch_start = datetime.now()
    reviews_count = 0

    # 병렬 실행을 위한 설정 - 더 많은 동시 요청으로 성능 향상
    MAX_WORKERS = min(50, len(pull_requests))  # GitHub 2차 레이트 제한 고려하면서 충분히 높게 설정
    print(f"리뷰 데이터 병렬 수집 시작 (최대 {MAX_WORKERS}개 스레드)")

    # 병렬 처리 결과를 저장할 컨테이너
    results = []

    # ThreadPoolExecutor를 사용한 병렬 처리
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 모든 PR에 대해 병렬로 리뷰 데이터 수집 작업 시작
        futures = {
            executor.submit(process_pr_reviews, pr, author_stats): (pr_index, pr)
            for pr_index, pr in enumerate(pull_requests, 1)
        }

        # 5개 단위로 진행 상황 표시
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            pr_index, pr = futures[future]
            if pr_index % 5 == 0 or pr_index == len(pull_requests):
                completed += 1
                print(f"PR 처리 중: {pr_index}/{len(pull_requests)}")

            try:
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
                    reviewer_stats[reviewer]["prs_reviewed"].update(
                        stats["prs_reviewed"]
                    )
                    reviewer_stats[reviewer]["overdue_count"] += stats["overdue_count"]

                # 작성자 통계에 리뷰 받은 PR 수 업데이트
                if has_reviews and pr.user:
                    author = pr.user.login
                    author_stats[author]["reviewed_prs"] += 1

            except Exception as e:
                print(f"PR {pr_index} 처리 중 오류 발생: {str(e)}")

    reviews_fetch_end = datetime.now()
    reviews_fetch_duration = (reviews_fetch_end - reviews_fetch_start).total_seconds()
    print(f"리뷰 데이터 병렬 조회 완료: {reviews_count}개 (소요 시간: {reviews_fetch_duration:.2f}초)")
    
    # 캐시 적중률 통계 출력
    if hasattr(get_pr_ready_time, 'ready_time_cache_hits'):
        hits = get_pr_ready_time.ready_time_cache_hits
        misses = get_pr_ready_time.ready_time_cache_misses
        total = hits + misses
        hit_rate = (hits / total * 100) if total > 0 else 0
        print(f"Ready 시간 캐시 통계: 적중 {hits}회, 실패 {misses}회, 적중률 {hit_rate:.1f}%")

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
    explanation = "• *평균응답*: PR 생성부터 첫 리뷰까지 평균 소요 시간\n• *24h초과*: 24시간 이상 소요된 리뷰 비율\n• *완료*: 완료한 리뷰 수\n• *대기*: 리뷰 요청 받았으나 아직 응답하지 않은 수"

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
    # 시간 계측 시작
    start_time = datetime.now()
    print(f"저장소 목록 조회 시작: {start_time.strftime('%H:%M:%S.%f')[:-3]}")

    # 최소 활동 기간 계산
    min_activity_date = datetime.now(timezone.utc) - timedelta(days=min_activity_days)

    # 조직의 모든 저장소 가져오기
    fetch_start = datetime.now()
    org = github_client.get_organization(org_name)
    all_repos = list(org.get_repos())  # 페이지네이션 완료를 위해 리스트로 변환
    fetch_end = datetime.now()

    fetch_duration = (fetch_end - fetch_start).total_seconds()
    print(
        f"총 {len(all_repos)}개 저장소 로드 완료: {fetch_end.strftime('%H:%M:%S.%f')[:-3]} (소요 시간: {fetch_duration:.2f}초)"
    )

    # 최근 활동이 있는 저장소만 필터링
    filter_start = datetime.now()
    active_repos = []
    repo_count = 0

    for repo in all_repos:
        repo_count += 1
        if repo_count % 10 == 0:
            print(f"저장소 검사 진행 중... {repo_count}/{len(all_repos)}")

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

    filter_end = datetime.now()
    filter_duration = (filter_end - filter_start).total_seconds()
    total_duration = (filter_end - start_time).total_seconds()

    print(
        f"활성 저장소 필터링 완료: {len(active_repos)}개 - {filter_end.strftime('%H:%M:%S.%f')[:-3]} (소요 시간: {filter_duration:.2f}초)"
    )
    print(f"저장소 목록 조회 총 소요 시간: {total_duration:.2f}초")

    # 활성 저장소 목록 바로 반환 (추가 API 호출 없이)
    # 이미 모든 정보를 저장하고 있기 때문에, 새로운 API 호출 없이 정렬을 수행
    print(f"활성 저장소 목록 반환 - 추가 정렬 없이 반환합니다.")
    return active_repos


def main():
    """
    GitHub PR 리뷰 통계를 수집하고 Slack에 전송합니다.

    --dry-run 옵션이 주어지면 실제 메시지 전송 없이 콘솔에만 출력합니다.
    """
    parser = argparse.ArgumentParser(description="GitHub PR 리뷰 통계 수집")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="몇 일간의 데이터를 조회할지 설정 (기본값: 7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="메시지를 Slack에 전송하지 않고 콘솔에만 출력합니다",
    )
    parser.add_argument(
        "--limit-repos",
        type=int,
        default=0,  # 0은 제한 없음을 의미
        help="분석할 최대 저장소 수 (기본값: 0=제한 없음)",
    )

    args = parser.parse_args()

    try:
        # GitHub API 초기화
        github_client = Github(GITHUB_TOKEN)

        # Slack API 초기화
        slack_client = WebClient(token=SLACK_BOT_TOKEN)

        # 조직의 활성 저장소 조회
        print(f"{ORG_NAME} 조직의 활성 저장소 조회 중...")
        repositories = get_active_repos(github_client, ORG_NAME)

        # 사용자가 명시적으로 제한한 경우에만 저장소 수 제한
        if args.limit_repos > 0:
            if len(repositories) > args.limit_repos:
                print(f"저장소 수를 사용자 지정 값 {args.limit_repos}개로 제한합니다.")
                repositories = repositories[: args.limit_repos]
        else:
            print(f"모든 활성 저장소({len(repositories)}개)를 분석합니다.")

        print(f"분석 대상 저장소 {len(repositories)}개를 찾았습니다.")

        # 병렬로 각 저장소의 PR을 가져옴
        all_pull_requests = []
        repo_stats = {}  # 저장소별 PR 수 추적
        pr_fetch_start = datetime.now()

        print(f"PR 데이터 병렬 수집 시작: {pr_fetch_start.strftime('%H:%M:%S.%f')[:-3]}")

        # 저장소 PR 병렬 조회를 위한 함수
        def fetch_repo_prs(repo_index, repo_full_name, days):
            repo_start_time = datetime.now()
            repo_owner, repo_name = repo_full_name.split("/")
            print(
                f"[{repo_index}/{len(repositories)}] {repo_full_name} 저장소의 최근 {days}일간 PR 조회 중..."
            )

            try:
                fetch_pr_start = datetime.now()
                repo_prs = fetch_pull_requests(
                    github_client, repo_owner, repo_name, days
                )
                fetch_pr_end = datetime.now()
                fetch_pr_duration = (fetch_pr_end - fetch_pr_start).total_seconds()

                repo_end_time = datetime.now()
                repo_duration = (repo_end_time - repo_start_time).total_seconds()

                print(
                    f"- {len(repo_prs)}개의 PR을 찾았습니다. (PR 수집: {fetch_pr_duration:.2f}초, 총 소요: {repo_duration:.2f}초)"
                )
                return repo_full_name, repo_prs
            except Exception as repo_error:
                print(f"- 오류 발생: {str(repo_error)}")
                return repo_full_name, []

        # 저장소 병렬 처리를 위한 설정
        REPO_MAX_WORKERS = min(30, len(repositories))  # 저장소 수에 따라 동적으로 조정

        # ThreadPoolExecutor를 사용한 병렬 처리
        with ThreadPoolExecutor(max_workers=REPO_MAX_WORKERS) as executor:
            # 모든 저장소에 대해 병렬로 PR 조회 시작
            futures = {
                executor.submit(
                    fetch_repo_prs, repo_index, repo_full_name, args.days
                ): repo_index
                for repo_index, repo_full_name in enumerate(repositories, 1)
            }

            # 결과 수집
            for future in concurrent.futures.as_completed(futures):
                repo_full_name, repo_prs = future.result()
                if repo_prs:  # 결과가 있는 경우만 추가
                    all_pull_requests.extend(repo_prs)
                    repo_stats[repo_full_name] = len(repo_prs)

        pr_fetch_end = datetime.now()
        pr_fetch_duration = (pr_fetch_end - pr_fetch_start).total_seconds()
        print(
            f"모든 PR 데이터 병렬 수집 완료: 총 {len(all_pull_requests)}개 (소요 시간: {pr_fetch_duration:.2f}초)"
        )

        if not all_pull_requests:
            print("분석할 PR이 없습니다.")
            return

        # 통계 계산
        print("리뷰 통계 계산 중...")
        stats_start = datetime.now()
        stats = calculate_stats(all_pull_requests)
        stats_end = datetime.now()
        stats_duration = (stats_end - stats_start).total_seconds()
        print(f"통계 계산 완료 (소요 시간: {stats_duration:.2f}초)")

        # 리뷰어 통계 표시
        reviewer_table = format_reviewer_table(stats["reviewers"])

        # 저장소별 통계
        repo_activity = "\n".join(
            [
                f"• {repo}: {count}개 PR"
                for repo, count in repo_stats.items()
                if count > 0
            ]
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
            print(f"Slack 채널 {SLACK_CHANNEL_ID}에 통계 전송 중...")
            # 저장소별 통계도 함께 전송
            stats["repo_stats"] = repo_stats
            send_to_slack(slack_client, SLACK_CHANNEL_ID, stats, args.days)
            print("전송 완료!")

    except Exception as e:
        error_msg = f"리뷰 통계 처리 중 오류가 발생했습니다: {str(e)}"
        print(f"오류: {error_msg}")


if __name__ == "__main__":
    main()
