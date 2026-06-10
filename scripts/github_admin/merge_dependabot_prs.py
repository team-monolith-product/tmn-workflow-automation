"""
Organization의 Dependabot PR 중 안전한 업데이트이면서 CI를 통과한 PR을 자동 병합하는 스크립트

GitHub Search API로 org 전체의 열린 Dependabot PR을 조회한 뒤 (레포 순회 없음),
커밋 메시지에 Dependabot이 심는 기계가독 메타데이터(updated-dependencies YAML
블록 + 의존성별 from/to 본문)로 버전 변경 폭을 판별하고, head 커밋에 존재하는
모든 체크가 통과했는지 확인하여 approve + 병합합니다.

병합 조건 (PR에 포함된 모든 의존성이 만족해야 함):
- fork가 아닌 레포여야 함 (fork는 테스트 셋업을 우리가 소유하지 않음)
- 커밋 메타데이터에서 변경 전/후 버전을 파싱할 수 있어야 함 (제거된 의존성,
  requirement 범위 변경 등 버전 쌍이 없는 항목은 보수적으로 차단)
- major 버전이 동일해야 함 (단, 0.x 버전은 semver 관례상 minor 변경도
  breaking으로 간주하여 minor까지 동일해야 함)
- head 커밋의 모든 체크가 완료 + 통과 상태이고, 성공으로 끝난 체크가
  1개 이상 존재해야 함 (체크가 없거나 전부 skipped인 레포는 차단)

리뷰 요건(ruleset의 required approving review)은 이 스크립트의 정식 approve로
충족되므로 bypass에 의존하지 않습니다.

사용법:
    python scripts/github_admin/merge_dependabot_prs.py [--dry-run]

옵션:
    --dry-run: 실제 approve/병합 없이 어떤 PR이 병합될지 확인
"""

import argparse
import os
import re
import sys
from typing import Literal

import yaml
from github import GithubException
from github.PullRequest import PullRequest

# 프로젝트 루트를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from scripts.github_admin.common import get_github_client, get_org_name

# check run conclusion 중 실패로 간주하는 값
FAILURE_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "stale"}

CiState = Literal["success", "pending", "failure", "none"]

# 커밋 메시지의 updated-dependencies YAML 블록 (--- 와 ... 사이)
UPDATED_DEPENDENCIES_PATTERN = re.compile(
    r"^---\n(updated-dependencies:\n.*?)^\.\.\.$", re.MULTILINE | re.DOTALL
)

# 커밋 메시지의 의존성별 버전 변경 줄
# 예: "Bumps [shell-quote](https://...) from 1.8.1 to 1.8.4."
#     "Updates `terser-webpack-plugin` from 5.3.16 to 5.6.1"
#     "chore(deps): bump jupyterlab from 4.1.8 to 4.5.7 in /jupyterlab4" (본문 없는 제목형)
VERSION_CHANGE_PATTERN = re.compile(
    r"(?:[Bb]umps?|[Uu]pdates?)\s+"
    r"(?:\[(?P<bracket>[^\]]+)\]\([^)]*\)|`(?P<tick>[^`]+)`|(?P<plain>\S+))\s+"
    r"from\s+(?P<before>\S+)\s+to\s+(?P<after>\S+)"
)


def parse_version(text: str) -> tuple[int, ...] | None:
    """
    문자열에서 버전 숫자 튜플을 추출하는 함수

    Args:
        text: "1.2.3", "v4" 등 버전을 포함한 문자열

    Returns:
        tuple[int, ...] | None: (major, minor, patch) 형태의 튜플. 파싱 불가 시 None
    """
    match = re.search(r"v?(\d+(?:\.\d+)*)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_minor_or_lower_update(before: tuple[int, ...], after: tuple[int, ...]) -> bool:
    """
    버전 변경이 minor 이하인지 판별하는 함수

    major 버전이 동일하면 minor 이하 업데이트로 간주합니다.
    0.x 버전은 semver 관례상 minor 변경도 breaking이므로 앞 두 자리가 같아야 합니다.

    Args:
        before: 변경 전 버전 튜플
        after: 변경 후 버전 튜플

    Returns:
        bool: minor 이하 업데이트면 True
    """
    if before[0] != after[0]:
        return False
    if before[0] == 0:
        return before[:2] == after[:2]
    return True


def parse_dependabot_commit(message: str) -> list[dict] | None:
    """
    Dependabot 커밋 메시지에서 의존성별 버전 변경 정보를 파싱하는 함수

    Dependabot은 모든 커밋에 updated-dependencies YAML 블록(의존성 목록의 정본)과
    의존성별 "... from X to Y" 본문 줄을 생성합니다. 그룹 업데이트도 동일합니다.

    Args:
        message: Dependabot 커밋 메시지 전문

    Returns:
        list[dict] | None: [{"name": str, "before": str | None, "after": str | None}]
            형태의 목록. YAML 블록이 없으면 None.
            본문에 버전 변경 줄이 없는 의존성(제거 등)은 before/after가 None.
    """
    yaml_match = UPDATED_DEPENDENCIES_PATTERN.search(message)
    if not yaml_match:
        return None

    dependencies = yaml.safe_load(yaml_match.group(1))["updated-dependencies"]

    versions: dict[str, tuple[str, str]] = {}
    for match in VERSION_CHANGE_PATTERN.finditer(message):
        name = match.group("bracket") or match.group("tick") or match.group("plain")
        versions[name] = (match.group("before"), match.group("after").rstrip(".,"))

    updates = []
    for dependency in dependencies:
        name = dependency["dependency-name"]
        before, after = versions.get(name, (None, None))
        updates.append({"name": name, "before": before, "after": after})
    return updates


def evaluate_updates(updates: list[dict] | None) -> tuple[bool, str]:
    """
    파싱된 의존성 변경 목록이 자동 병합 정책을 만족하는지 평가하는 순수 함수

    Args:
        updates: parse_dependabot_commit()의 결과

    Returns:
        tuple[bool, str]: (병합 가능 여부, 사유)
    """
    if not updates:
        return False, "커밋 메타데이터 파싱 불가"

    for update in updates:
        if update["before"] is None or update["after"] is None:
            return False, f"{update['name']}: 버전 쌍 없음 (제거 또는 범위 변경)"

        before = parse_version(update["before"])
        after = parse_version(update["after"])
        if before is None or after is None:
            return False, f"{update['name']}: 버전 파싱 불가"

        if not is_minor_or_lower_update(before, after):
            return (
                False,
                f"{update['name']}: 차단 대상 업데이트 "
                f"({update['before']} -> {update['after']})",
            )

    summary = ", ".join(f"{u['name']} {u['before']}->{u['after']}" for u in updates)
    return True, f"minor 이하 ({summary})"


def evaluate_ci(check_runs: list[tuple[str, str, str | None]]) -> CiState:
    """
    체크 결과 목록에서 CI 상태를 종합하는 순수 함수

    "성공으로 끝난 체크 1개 이상"을 실제 CI 존재의 증거로 요구합니다
    (체크가 없거나 전부 skipped인 경우는 CI 없음으로 간주).

    Args:
        check_runs: (이름, status, conclusion) 목록 (GitHub Actions 등 check run)

    Returns:
        CiState: "success" | "pending" | "failure" | "none" (CI 없음)
    """
    has_success = False
    has_pending = False

    for name, status, conclusion in check_runs:
        if status != "completed":
            has_pending = True
        elif conclusion in FAILURE_CONCLUSIONS:
            return "failure"
        elif conclusion == "success":
            has_success = True

    if has_pending:
        return "pending"
    if has_success:
        return "success"
    return "none"


def get_ci_state(pr: PullRequest) -> CiState:
    """
    PR head 커밋의 체크 결과를 조회하여 CI 상태를 반환하는 함수

    Args:
        pr: 대상 PR

    Returns:
        CiState: evaluate_ci()의 결과
    """
    commit = pr.base.repo.get_commit(pr.head.sha)
    check_runs = [
        (run.name, run.status, run.conclusion) for run in commit.get_check_runs()
    ]
    return evaluate_ci(check_runs)


def ensure_approved(pr: PullRequest, reviewer_login: str) -> None:
    """
    PR에 reviewer_login의 approve가 없으면 approve를 남기는 함수

    매 시행마다 중복 리뷰가 쌓이지 않도록 기존 approve를 확인합니다.

    Args:
        pr: 대상 PR
        reviewer_login: 이 스크립트가 사용하는 토큰의 사용자 로그인
    """
    for review in pr.get_reviews():
        if review.user.login == reviewer_login and review.state == "APPROVED":
            return
    pr.create_review(event="APPROVE")


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
        dry_run: True면 실제 approve/병합 없이 대상 PR만 출력
    """
    g = get_github_client()
    org_name = get_org_name()
    reviewer_login = g.get_user().login

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
            if pr.draft:
                print(f"[SKIP] {label}: draft PR - {pr.title}")
                skip_count += 1
                continue

            # fork는 테스트 셋업을 우리가 소유하지 않고 upstream에서 동기화되므로 제외
            if pr.base.repo.fork:
                print(f"[SKIP] {label}: fork 레포 - {pr.title}")
                skip_count += 1
                continue

            commit_message = pr.base.repo.get_commit(pr.head.sha).commit.message
            eligible, reason = evaluate_updates(parse_dependabot_commit(commit_message))
            if not eligible:
                print(f"[SKIP] {label}: {reason} - {pr.title}")
                skip_count += 1
                continue

            ci_state = get_ci_state(pr)
            if ci_state != "success":
                ci_reason = "CI 없음" if ci_state == "none" else f"CI {ci_state}"
                print(f"[SKIP] {label}: {ci_reason} - {pr.title}")
                skip_count += 1
                continue

            if dry_run:
                print(f"[DRY-RUN] {label}: 병합 예정 ({reason}) - {pr.title}")
            else:
                ensure_approved(pr, reviewer_login)
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
        help="실제 approve/병합 없이 어떤 PR이 병합될지 확인",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
