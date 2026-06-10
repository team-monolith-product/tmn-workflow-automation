"""
Dependabot 자동 병합 구성을 각 리포지토리에 동기화하는 스크립트

판단 로직은 전부 플랫폼 네이티브 기능에 위임하고, 이 스크립트는
dependabot_automerge_config.json에 선언된 상태를 동기화만 합니다.

리포지토리별 동기화 항목:
1. allow_auto_merge 설정 활성화
2. "Dependabot Auto-Merge CI Gate" ruleset (기본 브랜치에 required status checks)
3. caller 워크플로우 파일 (.github/workflows/dependabot-automerge.yml)
   - 실제 정책은 tmn-gh-actions의 reusable workflow에 있고 caller는 무로직

required_checks가 선언되지 않은 레포에는 caller를 배포하지 않으므로
"CI 없는 레포는 자동 병합 금지" 불변식이 구조적으로 보장됩니다.

사용법:
    python scripts/github_admin/sync_dependabot_automerge.py [--dry-run]

옵션:
    --dry-run: 실제 변경 없이 어떤 동기화가 수행될지 확인
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import requests
from github import GithubException
from github.Repository import Repository

# 프로젝트 루트를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from scripts.github_admin.add_ruleset import (
    add_ruleset,
    delete_ruleset,
    find_ruleset_by_name,
    get_headers,
)
from scripts.github_admin.common import (
    get_github_client,
    get_org_name,
    get_organization,
)

SCRIPT_DIR = Path(__file__).parent

CONFIG_FILE = "dependabot_automerge_config.json"

RULESET_NAME = "Dependabot Auto-Merge CI Gate"

CALLER_PATH = ".github/workflows/dependabot-automerge.yml"

CALLER_CONTENT = """\
# tmn-workflow-automation의 sync_dependabot_automerge.py가 동기화하는 파일입니다.
# 직접 수정하지 마세요. 정책은 tmn-gh-actions의 reusable workflow에 있습니다.
name: Dependabot Auto-Merge

on: pull_request

permissions:
  contents: write
  pull-requests: write

jobs:
  automerge:
    if: github.event.pull_request.user.login == 'dependabot[bot]'
    uses: team-monolith-product/tmn-gh-actions/.github/workflows/dependabot-automerge.yml@main
"""


def load_automerge_config() -> dict[str, dict]:
    """
    dependabot_automerge_config.json을 로드하고 검증하는 함수

    Returns:
        dict: repo 이름 → {"required_checks": [...]} 매핑

    Raises:
        ValueError: required_checks가 비어 있는 항목이 있는 경우
    """
    config_path = SCRIPT_DIR / CONFIG_FILE
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    for repo_name, repo_config in config.items():
        if not repo_config.get("required_checks"):
            raise ValueError(
                f'"{repo_name}"에 required_checks가 없습니다. '
                "CI 게이트 없는 자동 병합은 허용되지 않습니다."
            )
    return config


def build_ci_gate_ruleset(required_checks: list[str]) -> dict:
    """
    기본 브랜치에 required status checks를 강제하는 ruleset을 생성하는 함수

    Args:
        required_checks: 병합 전 통과해야 하는 check 이름 목록

    Returns:
        dict: Ruleset API 요청 본문
    """
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
        },
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [
                        {"context": check} for check in required_checks
                    ],
                },
            }
        ],
    }


def get_ruleset_required_checks(
    org_name: str, repo_name: str, ruleset_id: int
) -> set[str]:
    """
    기존 ruleset에 선언된 required check 이름 집합을 가져오는 함수

    Args:
        org_name: Organization 이름
        repo_name: 리포지토리 이름
        ruleset_id: 조회할 ruleset의 ID

    Returns:
        set: required check 이름 집합
    """
    url = f"https://api.github.com/repos/{org_name}/{repo_name}/rulesets/{ruleset_id}"
    response = requests.get(url, headers=get_headers(), timeout=30)
    response.raise_for_status()

    checks: set[str] = set()
    for rule in response.json().get("rules", []):
        if rule["type"] == "required_status_checks":
            for check in rule["parameters"]["required_status_checks"]:
                checks.add(check["context"])
    return checks


def sync_auto_merge_setting(repo: Repository, dry_run: bool) -> str:
    """
    리포지토리의 allow_auto_merge 설정을 동기화하는 함수

    Args:
        repo: 대상 리포지토리
        dry_run: dry-run 모드 여부

    Returns:
        str: 수행한 동작 설명
    """
    if repo.allow_auto_merge:
        return "allow_auto_merge 이미 설정됨"
    if dry_run:
        return "allow_auto_merge 활성화 예정"
    repo.edit(allow_auto_merge=True)
    return "allow_auto_merge 활성화"


def sync_ci_gate_ruleset(
    org_name: str, repo_name: str, required_checks: list[str], dry_run: bool
) -> str:
    """
    CI 게이트 ruleset을 동기화하는 함수

    required check 목록이 config와 다르면 삭제 후 재생성합니다.

    Args:
        org_name: Organization 이름
        repo_name: 리포지토리 이름
        required_checks: 강제할 check 이름 목록
        dry_run: dry-run 모드 여부

    Returns:
        str: 수행한 동작 설명
    """
    exists, ruleset_id = find_ruleset_by_name(org_name, repo_name, RULESET_NAME)

    if exists and ruleset_id:
        current = get_ruleset_required_checks(org_name, repo_name, ruleset_id)
        if current == set(required_checks):
            return "ruleset 이미 일치"
        if dry_run:
            return f"ruleset 갱신 예정 ({sorted(current)} -> {required_checks})"
        delete_ruleset(org_name, repo_name, ruleset_id)
        add_ruleset(org_name, repo_name, build_ci_gate_ruleset(required_checks))
        return "ruleset 갱신"

    if dry_run:
        return f"ruleset 생성 예정 (required: {required_checks})"
    add_ruleset(org_name, repo_name, build_ci_gate_ruleset(required_checks))
    return "ruleset 생성"


def sync_caller_workflow(repo: Repository, dry_run: bool) -> str:
    """
    caller 워크플로우 파일을 동기화하는 함수

    Args:
        repo: 대상 리포지토리
        dry_run: dry-run 모드 여부

    Returns:
        str: 수행한 동작 설명
    """
    commit_message = "ci: Dependabot 자동 병합 caller 워크플로우 동기화"

    try:
        existing = repo.get_contents(CALLER_PATH)
    except GithubException as e:
        if e.status != 404:
            raise
        if dry_run:
            return "caller 워크플로우 생성 예정"
        repo.create_file(CALLER_PATH, commit_message, CALLER_CONTENT)
        return "caller 워크플로우 생성"

    current_content = base64.b64decode(existing.content).decode("utf-8")
    if current_content == CALLER_CONTENT:
        return "caller 워크플로우 이미 일치"
    if dry_run:
        return "caller 워크플로우 갱신 예정"
    repo.update_file(CALLER_PATH, commit_message, CALLER_CONTENT, existing.sha)
    return "caller 워크플로우 갱신"


def main(dry_run: bool = False):
    """
    config에 선언된 모든 리포지토리에 자동 병합 구성을 동기화하는 메인 함수

    Args:
        dry_run: True면 실제 변경 없이 수행될 동작만 출력
    """
    config = load_automerge_config()
    g = get_github_client()
    org_name = get_org_name()
    org = get_organization(g, org_name)

    print(f"Organization: {org_name}")
    print(f"대상 리포지토리: {len(config)}개")
    print("=" * 60)

    error_count = 0

    for repo_name, repo_config in config.items():
        print(f"\n[{repo_name}]")
        try:
            repo = org.get_repo(repo_name)
            results = [
                sync_auto_merge_setting(repo, dry_run),
                sync_ci_gate_ruleset(
                    org_name, repo_name, repo_config["required_checks"], dry_run
                ),
                sync_caller_workflow(repo, dry_run),
            ]
            for result in results:
                print(f"  - {result}")
        except (GithubException, requests.exceptions.RequestException) as e:
            print(f"  [ERROR] {e}")
            error_count += 1

    print("\n" + "=" * 60)
    print(f"완료: 대상 {len(config)}, 오류 {error_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dependabot 자동 병합 구성을 config에 선언된 리포지토리에 동기화합니다."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 변경 없이 어떤 동기화가 수행될지 확인",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
