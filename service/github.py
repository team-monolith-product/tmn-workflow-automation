"""
GitHub API 관련 서비스 레이어입니다.
GitHub API 요청의 병렬 처리와 최적화를 담당합니다.
"""

import concurrent.futures
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# PyGithub 라이브러리에서 필요한 클래스 임포트
from github import Github
from github.PullRequest import PullRequest
from github.TimelineEvent import TimelineEvent
from github.PullRequestComment import PullRequestComment


def fetch_pull_requests_parallel(
    github_client: Github, repositories: list[str], since_date: datetime
) -> dict[str, list[PullRequest]]:
    """
    여러 저장소의 PR을 병렬로 가져옵니다.

    Args:
        github_client: GitHub API 클라이언트
        repositories: 저장소 목록 (format: "owner/name")
        since_date: 이 날짜 이후의 PR만 가져옴 (timezone 정보 포함 필요)

    Returns:
        dict[str, list[PullRequest]]: 저장소별 PR 목록을 담은 딕셔너리 (키: 저장소명, 값: PR 목록)
    """
    # 병렬 처리를 위한 최대 워커 수 설정
    REPO_MAX_WORKERS = min(30, len(repositories))
    repository_to_pull_requests = {}  # 저장소별 PR 목록

    # 단일 저장소의 PR을 가져오는 내부 함수
    def fetch_repo_prs(repo_full_name):
        repo_owner, repo_name = repo_full_name.split("/")

        # 저장소 접근
        repo = github_client.get_repo(f"{repo_owner}/{repo_name}")

        # PR 조회: 모든 상태의 PR을 일괄로 가져옴
        all_pulls = []

        # 모든 PR을 업데이트 날짜 기준 내림차순으로 가져옴 (가장 최근 항목부터)
        all_prs_iterator = repo.get_pulls(state="all", sort="updated", direction="desc")

        # 필요한 만큼만 가져오기 - 페이지네이션 최소화
        for pr in all_prs_iterator:
            # 날짜가 범위를 벗어나면 중단 (업데이트 순으로 정렬되어 있으므로 최적화 가능)
            if pr.updated_at < since_date and pr.created_at < since_date:
                break

            # PR을 결과 목록에 추가
            all_pulls.append(pr)

        return repo_full_name, all_pulls

    # 병렬 처리
    with ThreadPoolExecutor(max_workers=REPO_MAX_WORKERS) as executor:
        futures = [
            executor.submit(fetch_repo_prs, repo_full_name)
            for repo_full_name in repositories
        ]

        for future in concurrent.futures.as_completed(futures):
            repo_full_name, repo_prs = future.result()
            if repo_prs:  # 결과가 있는 경우만 추가
                repository_to_pull_requests[repo_full_name] = repo_prs

    return repository_to_pull_requests


def fetch_pr_timeline_events_parallel(
    pull_requests: list[PullRequest],
) -> dict[int, list[TimelineEvent]]:
    """
    여러 PR의 타임라인 이벤트를 병렬로 가져옵니다.

    Args:
        pull_requests: PR 객체 목록

    Returns:
        dict[int, list[TimelineEvent]]: PR의 고유 ID와 관련 타임라인 이벤트 객체 목록을 매핑한 딕셔너리
    """
    # 병렬 처리를 위한 최대 워커 수 설정 - 수행할 PR 개수와 CPU 코어 기반 최적화
    MAX_WORKERS = min(50, len(pull_requests))

    # 각 PR의 타임라인 이벤트를 가져오는 함수
    def fetch_pr_timeline(pr):
        # PR의 고유 ID를 키로 사용
        pr_id = pr.id

        # PR을 Issue로 변환하여 타임라인에 접근
        issue = pr.as_issue()
        timeline = issue.get_timeline()

        # 원본 타임라인 이벤트 객체들을 리스트로 수집
        events = list(timeline)

        return pr_id, events

    # 병렬 처리
    pr_id_to_events = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 모든 PR에 대해 병렬로 타임라인 이벤트 로드
        for pr_id, events in executor.map(fetch_pr_timeline, pull_requests):
            pr_id_to_events[pr_id] = events

    return pr_id_to_events


def fetch_pr_review_comments_parallel(
    pull_requests: list[PullRequest],
) -> dict[int, list[PullRequestComment]]:
    """
    여러 PR의 리뷰 댓글을 병렬로 가져옵니다.

    Args:
        pull_requests: PR 객체 목록

    Returns:
        dict[int, list[PullRequestComment]]: PR의 고유 ID와 관련 리뷰 댓글을 매핑한 딕셔너리
    """
    # 병렬 처리를 위한 최대 워커 수 설정
    MAX_WORKERS = min(50, len(pull_requests))

    # 각 PR의 리뷰 댓글을 가져오는 함수
    def fetch_pr_comments(pr):
        return pr.id, list(pr.get_review_comments())

    # 병렬 처리
    pr_id_to_comments = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 모든 PR에 대해 병렬로 리뷰 댓글 로드
        for pr_id, comments in executor.map(fetch_pr_comments, pull_requests):
            pr_id_to_comments[pr_id] = comments

    return pr_id_to_comments
