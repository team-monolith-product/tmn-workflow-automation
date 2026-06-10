"""
Organization의 Dependabot PR 중 minor 이하 업데이트이면서 CI를 통과한 PR을 자동 병합하는 스크립트

GitHub Search API로 org 전체의 열린 Dependabot PR을 조회한 뒤,
PR 제목에서 버전 변경 폭을 판별하고 head 커밋의 CI 결과를 확인하여 병합합니다.

병합 조건:
- PR 제목에서 before/after 버전을 파싱할 수 있어야 함 (그룹 업데이트 PR은 제외)
- major 버전이 동일해야 함 (단, 0.x 버전은 minor까지 동일해야 함)
- head 커밋에 check run 또는 commit status가 1개 이상 존재하고 모두 통과해야 함

사용법:
    python scripts/github_admin/merge_dependabot_prs.py [--dry-run]

옵션:
    --dry-run: 실제 병합 없이 어떤 PR이 병합될지 확인
"""

import argparse
import os
import re
import sys

from github import GithubException
from github.Commit import Commit
from github.PullRequest import PullRequest

# 프로젝트 루트를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from scripts.github_admin.common import get_github_client, get_org_name

# check run conclusion 중 실패로 간주하는 값
FAILURE_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "stale"}


def parse_version(text: str) -> tuple[int, ...] | None:
    """
    문자열에서 버전 숫자 튜플을 추출하는 함수

    Args:
        text: "1.2.3", "v4", "~> 7.0" 등 버전을 포함한 문자열

    Returns:
        tuple[int, ...] | None: (major, minor, patch) 형태의 튜플. 파싱 불가 시 None
    """
    match = re.search(r"v?(\d+(?:\.\d+)*)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def parse_update_versions(title: str) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """
    Dependabot PR 제목에서 변경 전/후 버전을 파싱하는 함수

    "Bump foo from 1.2.3 to 1.4.0", "Update bar requirement from ~> 7.0 to ~> 7.1"
    형태를 지원합니다. 그룹 업데이트 제목("Bump the npm group with 3 updates")은
    버전 정보가 없으므로 None을 반환합니다.

    Args:
        title: PR 제목

    Returns:
        tuple | None: (before, after) 버전 튜플 쌍. 파싱 불가 시 None
    """
    match = re.search(r" from (?P<before>.+) to (?P<after>.+)$", title)
    if not match:
        return None

    before = parse_version(match.group("before"))
    after = parse_version(match.group("after"))
    if before is None or after is None:
        return None
    return before, after


def is_minor_or_lower_update(before: tuple[int, ...], after: tuple[int, ...]) -> bool:
    """
    버전 변경이 minor 이하인지 판별하는 함수

    major 버전이 동일하면 minor 이하 업데이트로 간주합니다.
    0.x 버전은 semver 관례상 minor 변경도 breaking이므로 minor까지 동일해야 합니다.

    Args:
        before: 변경 전 버전 튜플
        after: 변경 후 버전 튜플

    Returns:
        bool: minor 이하 업데이트면 True
    """
    if before[0] != after[0]:
        return False
    if before[0] == 0:
        return before[1:2] == after[1:2]
    return True


def get_ci_state(commit: Commit) -> str:
    """
    커밋의 CI 결과를 종합하여 상태를 반환하는 함수

    GitHub Actions 등의 check run과 외부 CI의 commit status를 모두 확인합니다.

    Args:
        commit: PR head 커밋

    Returns:
        str: "success" | "pending" | "failure" | "none" (CI 없음)
    """
    states: set[str] = set()

    for run in commit.get_check_runs():
        if run.status != "completed":
            states.add("pending")
        elif run.conclusion in FAILURE_CONCLUSIONS:
            states.add("failure")
        else:
            states.add("success")

    combined = commit.get_combined_status()
    if combined.total_count > 0:
        states.add(combined.state)

    if not states:
        return "none"
    if "failure" in states or "error" in states:
        return "failure"
    if "pending" in states:
        return "pending"
    return "success"


def evaluate_pr(pr: PullRequest) -> tuple[bool, str]:
    """
    Dependabot PR이 자동 병합 대상인지 평가하는 함수

    Args:
        pr: 평가할 PR

    Returns:
        tuple[bool, str]: (병합 가능 여부, 사유)
    """
    if pr.draft:
        return False, "draft PR"

    versions = parse_update_versions(pr.title)
    if versions is None:
        return False, "버전 파싱 불가 (그룹 업데이트 등)"

    before, after = versions
    if not is_minor_or_lower_update(before, after):
        return (
            False,
            f"major 업데이트 ({format_version(before)} -> {format_version(after)})",
        )

    ci_state = get_ci_state(pr.head.repo.get_commit(pr.head.sha))
    if ci_state == "none":
        return False, "CI 없음"
    if ci_state != "success":
        return False, f"CI {ci_state}"

    return (
        True,
        f"minor 이하 ({format_version(before)} -> {format_version(after)}) + CI 통과",
    )


def format_version(version: tuple[int, ...]) -> str:
    """버전 튜플을 점으로 구분된 문자열로 변환하는 함수"""
    return ".".join(str(part) for part in version)


def merge_pr(pr: PullRequest) -> None:
    """
    PR을 병합하는 함수

    리포지토리가 squash 병합을 허용하면 squash, 아니면 merge 방식을 사용합니다.

    Args:
        pr: 병합할 PR
    """
    merge_method = "squash" if pr.base.repo.allow_squash_merge else "merge"
    pr.merge(merge_method=merge_method)


def main(dry_run: bool = False):
    """
    Org 전체의 Dependabot PR을 조회하여 조건을 만족하는 PR을 병합하는 메인 함수

    Args:
        dry_run: True면 실제 병합 없이 대상 PR만 출력
    """
    g = get_github_client()
    org_name = get_org_name()

    issues = g.search_issues(
        f"org:{org_name} is:pr is:open author:app/dependabot archived:false"
    )

    merged_count = 0
    skip_count = 0
    error_count = 0

    for issue in issues:
        pr = issue.as_pull_request()
        label = f"{pr.base.repo.full_name}#{pr.number}"

        try:
            should_merge, reason = evaluate_pr(pr)
            if not should_merge:
                print(f"[SKIP] {label}: {reason} - {pr.title}")
                skip_count += 1
                continue

            if dry_run:
                print(f"[DRY-RUN] {label}: 병합 예정 ({reason}) - {pr.title}")
            else:
                merge_pr(pr)
                print(f"[MERGED] {label}: {reason} - {pr.title}")
            merged_count += 1

        except GithubException as e:
            print(f"[ERROR] {label}: {e.data.get('message', str(e))}")
            error_count += 1

    print("-" * 50)
    print(f"완료: 병합 {merged_count}, 스킵 {skip_count}, 오류 {error_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dependabot PR 중 minor 이하 + CI 통과 PR을 자동 병합합니다."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 병합 없이 어떤 PR이 병합될지 확인",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
