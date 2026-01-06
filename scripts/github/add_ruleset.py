"""
GitHub Organization의 모든 리포지토리에 Branch Protection Ruleset을 적용하는 스크립트

Ruleset API는 PyGithub에서 직접 지원하지 않으므로 REST API를 사용합니다.
인증은 환경변수의 토큰을 사용하여 보안을 유지합니다.

사용법:
    python scripts/github/add_ruleset.py [--dry-run] [--force]

옵션:
    --dry-run: 실제 변경 없이 어떤 리포지토리에 ruleset이 적용될지 확인
    --force: 기존 ruleset이 있어도 삭제 후 재적용
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

RULESET_NAME = "Main Protection By Security"
SCRIPT_DIR = Path(__file__).parent


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


def load_ruleset_template() -> dict:
    """
    ruleset.json 템플릿을 로드하는 함수

    Returns:
        dict: Ruleset 설정 딕셔너리

    Raises:
        FileNotFoundError: ruleset.json 파일이 없는 경우
    """
    ruleset_path = SCRIPT_DIR / "ruleset.json"
    if not ruleset_path.exists():
        raise FileNotFoundError(f"ruleset.json 파일을 찾을 수 없습니다: {ruleset_path}")

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


def ruleset_exists(org_name: str, repo_name: str) -> tuple[bool, int | None]:
    """
    특정 이름의 ruleset이 이미 존재하는지 확인하는 함수

    Args:
        org_name: Organization 이름
        repo_name: 리포지토리 이름

    Returns:
        tuple: (존재 여부, ruleset ID 또는 None)
    """
    rulesets = get_rulesets(org_name, repo_name)
    for ruleset in rulesets:
        if ruleset.get("name") == RULESET_NAME:
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


def main():
    parser = argparse.ArgumentParser(
        description="Organization의 모든 리포지토리에 Branch Protection Ruleset을 적용합니다."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 변경 없이 어떤 리포지토리에 ruleset이 적용될지 확인",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="기존 ruleset이 있어도 삭제 후 재적용",
    )
    args = parser.parse_args()

    # Ruleset 템플릿 로드
    try:
        ruleset_template = load_ruleset_template()
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    # GitHub 클라이언트 초기화
    g = get_github_client()
    org_name = get_org_name()
    org = get_organization(g, org_name)

    print(f"Organization: {org_name}")
    print(f"Ruleset 이름: {RULESET_NAME}")
    print(f"강제 재적용: {'예' if args.force else '아니오'}")
    print("-" * 50)

    success_count = 0
    skip_count = 0
    error_count = 0

    for repo in get_all_repos(org):
        repo_name = repo.name
        try:
            exists, ruleset_id = ruleset_exists(org_name, repo_name)

            if exists and not args.force:
                print(f"[SKIP] {repo_name}: Ruleset 이미 존재 (ID: {ruleset_id})")
                skip_count += 1
                continue

            if args.dry_run:
                action = "재적용 예정" if exists else "추가 예정"
                print(f"[DRY-RUN] {repo_name}: Ruleset {action}")
                success_count += 1
                continue

            # 기존 ruleset 삭제 (force 모드)
            if exists and ruleset_id:
                delete_ruleset(org_name, repo_name, ruleset_id)
                print(f"[DELETE] {repo_name}: 기존 Ruleset 삭제 (ID: {ruleset_id})")

            # 새 ruleset 추가
            add_ruleset(org_name, repo_name, ruleset_template)
            action = "재적용 완료" if exists else "추가 완료"
            print(f"[SUCCESS] {repo_name}: Ruleset {action}")
            success_count += 1

        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                try:
                    error_msg = e.response.json().get("message", str(e))
                except (ValueError, KeyError):
                    pass
            print(f"[ERROR] {repo_name}: {error_msg}")
            error_count += 1

    print("-" * 50)
    print(f"완료: 성공 {success_count}, 스킵 {skip_count}, 오류 {error_count}")


if __name__ == "__main__":
    main()
