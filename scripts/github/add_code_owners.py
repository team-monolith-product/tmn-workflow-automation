"""
GitHub Organization의 모든 리포지토리에 CODEOWNERS 파일을 추가하는 스크립트

사용법:
    python scripts/github/add_code_owners.py [--dry-run] [--team TEAM_NAME]

옵션:
    --dry-run: 실제 변경 없이 어떤 리포지토리에 CODEOWNERS가 추가될지 확인
    --team: CODEOWNERS에 지정할 팀 이름 (기본값: Security)
"""

import argparse
import os
import sys

from github import GithubException
from github.Repository import Repository

# 프로젝트 루트를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from scripts.github.common import (
    get_all_repos,
    get_github_client,
    get_org_name,
    get_organization,
)

CODEOWNERS_PATH = ".github/CODEOWNERS"


def get_codeowners_content(org_name: str, team_name: str) -> str:
    """
    CODEOWNERS 파일 내용을 생성하는 함수

    Args:
        org_name: Organization 이름
        team_name: 팀 이름

    Returns:
        str: CODEOWNERS 파일 내용
    """
    return f"* @{org_name}/{team_name}"


def codeowners_exists(repo: Repository) -> bool:
    """
    리포지토리에 CODEOWNERS 파일이 존재하는지 확인하는 함수

    Args:
        repo: PyGithub Repository 객체

    Returns:
        bool: CODEOWNERS 파일이 존재하면 True
    """
    try:
        repo.get_contents(CODEOWNERS_PATH)
        return True
    except GithubException as e:
        if e.status == 404:
            return False
        raise


def create_codeowners_file(
    repo: Repository, content: str, branch: str = "main"
) -> bool:
    """
    리포지토리에 CODEOWNERS 파일을 생성하는 함수

    Args:
        repo: PyGithub Repository 객체
        content: CODEOWNERS 파일 내용
        branch: 대상 브랜치 (기본값: main)

    Returns:
        bool: 성공 시 True
    """
    try:
        repo.create_file(
            path=CODEOWNERS_PATH,
            message="Add CODEOWNERS file",
            content=content,
            branch=branch,
        )
        return True
    except GithubException as e:
        # 기본 브랜치가 main이 아닐 경우 master로 재시도
        if e.status == 404 and branch == "main":
            return create_codeowners_file(repo, content, branch="master")
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Organization의 모든 리포지토리에 CODEOWNERS 파일을 추가합니다."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 변경 없이 어떤 리포지토리에 CODEOWNERS가 추가될지 확인",
    )
    parser.add_argument(
        "--team",
        default="Security",
        help="CODEOWNERS에 지정할 팀 이름 (기본값: Security)",
    )
    args = parser.parse_args()

    # GitHub 클라이언트 초기화
    g = get_github_client()
    org_name = get_org_name()
    org = get_organization(g, org_name)

    # CODEOWNERS 파일 내용 생성
    codeowners_content = get_codeowners_content(org_name, args.team)

    print(f"Organization: {org_name}")
    print(f"CODEOWNERS 내용: {codeowners_content}")
    print("-" * 50)

    success_count = 0
    skip_count = 0
    error_count = 0

    for repo in get_all_repos(org):
        repo_name = repo.name
        try:
            if codeowners_exists(repo):
                print(f"[SKIP] {repo_name}: CODEOWNERS 이미 존재")
                skip_count += 1
                continue

            if args.dry_run:
                print(f"[DRY-RUN] {repo_name}: CODEOWNERS 추가 예정")
                success_count += 1
            else:
                create_codeowners_file(repo, codeowners_content)
                print(f"[SUCCESS] {repo_name}: CODEOWNERS 추가 완료")
                success_count += 1

        except GithubException as e:
            print(f"[ERROR] {repo_name}: {e.data.get('message', str(e))}")
            error_count += 1

    print("-" * 50)
    print(f"완료: 성공 {success_count}, 스킵 {skip_count}, 오류 {error_count}")


if __name__ == "__main__":
    main()
