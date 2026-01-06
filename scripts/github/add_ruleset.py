"""
GitHub Organization의 모든 리포지토리에 Branch Protection Ruleset을 적용하는 스크립트

Ruleset API는 PyGithub에서 직접 지원하지 않으므로 REST API를 사용합니다.
인증은 환경변수의 토큰을 사용하여 보안을 유지합니다.

사용법:
    python scripts/github/add_ruleset.py [--dry-run]

옵션:
    --dry-run: 실제 변경 없이 어떤 리포지토리에 ruleset이 적용될지 확인

동작:
    - ruleset이 없으면 추가
    - ruleset이 있으면 삭제 후 재적용 (항상 덮어쓰기)

적용되는 Ruleset:
    - Main Protection: 기본 브랜치 보호 (PR 필수, force push 금지)
    - Develop Protection: develop 브랜치 보호 (일반 push 허용, force push 금지)
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

# 프로젝트 루트를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from scripts.github.common import (
    get_all_repos,
    get_github_client,
    get_org_name,
    get_organization,
)

# .env 파일 로드
load_dotenv()

SCRIPT_DIR = Path(__file__).parent

# 사용 가능한 ruleset 정의
AVAILABLE_RULESETS = {
    "main": "ruleset.json",
    "develop": "ruleset_develop.json",
}


def get_headers() -> dict[str, str]:
    """
    GitHub API 요청에 사용할 헤더를 생성하는 함수

    Returns:
        dict: Authorization과 Accept 헤더가 포함된 딕셔너리

    Raises:
        ValueError: GITHUB_ADMIN_TOKEN이 설정되지 않은 경우
    """
    token = os.getenv("GITHUB_ADMIN_TOKEN")
    if not token:
        raise ValueError("GITHUB_ADMIN_TOKEN 환경변수가 설정되지 않았습니다.")

    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_ruleset_template(ruleset_file: str) -> dict:
    """
    ruleset 템플릿을 로드하는 함수

    Args:
        ruleset_file: ruleset 파일 이름

    Returns:
        dict: Ruleset 설정 딕셔너리

    Raises:
        FileNotFoundError: ruleset 파일이 없는 경우
    """
    ruleset_path = SCRIPT_DIR / ruleset_file
    if not ruleset_path.exists():
        raise FileNotFoundError(f"Ruleset 파일을 찾을 수 없습니다: {ruleset_path}")

    with open(ruleset_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_rulesets(org_name: str, repo_name: str) -> list[dict]:
    """
    리포지토리의 모든 ruleset을 가져오는 함수

    Args:
        org_name: Organization 이름
        repo_name: 리포지토리 이름

    Returns:
        list: Ruleset 목록
    """
    url = f"https://api.github.com/repos/{org_name}/{repo_name}/rulesets"
    response = requests.get(url, headers=get_headers(), timeout=30)

    if response.status_code == 200:
        return response.json()
    elif response.status_code == 404:
        return []
    else:
        response.raise_for_status()
    return []


def find_ruleset_by_name(
    org_name: str, repo_name: str, ruleset_name: str
) -> tuple[bool, int | None]:
    """
    특정 이름의 ruleset이 이미 존재하는지 확인하는 함수

    Args:
        org_name: Organization 이름
        repo_name: 리포지토리 이름
        ruleset_name: 찾을 ruleset 이름

    Returns:
        tuple: (존재 여부, ruleset ID 또는 None)
    """
    rulesets = get_rulesets(org_name, repo_name)
    for ruleset in rulesets:
        if ruleset.get("name") == ruleset_name:
            return True, ruleset.get("id")
    return False, None


def delete_ruleset(org_name: str, repo_name: str, ruleset_id: int) -> bool:
    """
    Ruleset을 삭제하는 함수

    Args:
        org_name: Organization 이름
        repo_name: 리포지토리 이름
        ruleset_id: 삭제할 ruleset의 ID

    Returns:
        bool: 성공 시 True
    """
    url = f"https://api.github.com/repos/{org_name}/{repo_name}/rulesets/{ruleset_id}"
    response = requests.delete(url, headers=get_headers(), timeout=30)
    return response.status_code == 204


def add_ruleset(org_name: str, repo_name: str, ruleset: dict) -> dict:
    """
    Ruleset을 추가하는 함수

    Args:
        org_name: Organization 이름
        repo_name: 리포지토리 이름
        ruleset: Ruleset 설정

    Returns:
        dict: 생성된 ruleset 정보
    """
    url = f"https://api.github.com/repos/{org_name}/{repo_name}/rulesets"

    # 리포지토리별로 동적 필드 제거 (새로 생성 시 불필요)
    payload = {k: v for k, v in ruleset.items() if k not in ["id", "source", "source_type"]}

    response = requests.post(url, json=payload, headers=get_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def apply_ruleset_to_repos(
    org, org_name: str, ruleset_template: dict, dry_run: bool
) -> tuple[int, int]:
    """
    모든 리포지토리에 ruleset을 적용하는 함수 (항상 덮어쓰기)

    Args:
        org: PyGithub Organization 객체
        org_name: Organization 이름
        ruleset_template: Ruleset 설정 딕셔너리
        dry_run: dry-run 모드 여부

    Returns:
        tuple: (성공 수, 오류 수)
    """
    ruleset_name = ruleset_template["name"]
    success_count = 0
    error_count = 0

    for repo in get_all_repos(org):
        repo_name = repo.name
        try:
            exists, ruleset_id = find_ruleset_by_name(org_name, repo_name, ruleset_name)

            if dry_run:
                action = "덮어쓰기 예정" if exists else "추가 예정"
                print(f"  [DRY-RUN] {repo_name}: {action}")
                success_count += 1
                continue

            # 기존 ruleset 삭제 (있으면)
            if exists and ruleset_id:
                delete_ruleset(org_name, repo_name, ruleset_id)

            # 새 ruleset 추가
            add_ruleset(org_name, repo_name, ruleset_template)
            action = "덮어쓰기 완료" if exists else "추가 완료"
            print(f"  [SUCCESS] {repo_name}: {action}")
            success_count += 1

        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                try:
                    error_msg = e.response.json().get("message", str(e))
                except (ValueError, KeyError):
                    pass
            print(f"  [ERROR] {repo_name}: {error_msg}")
            error_count += 1

    return success_count, error_count


def main():
    parser = argparse.ArgumentParser(
        description="Organization의 모든 리포지토리에 Branch Protection Ruleset을 적용합니다."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 변경 없이 어떤 리포지토리에 ruleset이 적용될지 확인",
    )
    args = parser.parse_args()

    # 모든 ruleset 적용
    rulesets_to_apply = list(AVAILABLE_RULESETS.items())

    # Ruleset 템플릿 로드
    ruleset_templates = []
    for ruleset_key, ruleset_file in rulesets_to_apply:
        try:
            template = load_ruleset_template(ruleset_file)
            ruleset_templates.append((ruleset_key, template))
        except FileNotFoundError as e:
            print(f"[ERROR] {e}")
            sys.exit(1)

    # GitHub 클라이언트 초기화
    g = get_github_client()
    org_name = get_org_name()
    org = get_organization(g, org_name)

    print(f"Organization: {org_name}")
    print(f"적용할 Ruleset: {', '.join([t[1]['name'] for t in ruleset_templates])}")
    print("=" * 60)

    total_success = 0
    total_error = 0

    for ruleset_key, ruleset_template in ruleset_templates:
        ruleset_name = ruleset_template["name"]
        print(f"\n[Ruleset: {ruleset_name}]")
        print("-" * 50)

        success, error = apply_ruleset_to_repos(
            org, org_name, ruleset_template, args.dry_run
        )
        total_success += success
        total_error += error

        print(f"  소계: 성공 {success}, 오류 {error}")

    print("=" * 60)
    print(f"총계: 성공 {total_success}, 오류 {total_error}")


if __name__ == "__main__":
    main()
