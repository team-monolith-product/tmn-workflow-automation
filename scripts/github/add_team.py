"""
GitHub Organization의 모든 리포지토리에 특정 팀을 추가하는 스크립트

사용법:
    python scripts/github/add_team.py [--dry-run] [--team TEAM_SLUG] [--permission PERMISSION]

옵션:
    --dry-run: 실제 변경 없이 어떤 리포지토리에 팀이 추가될지 확인
    --team: 추가할 팀의 slug (기본값: security)
    --permission: 팀에 부여할 권한 (pull, push, admin, maintain, triage 중 선택, 기본값: push)
"""
import argparse
import os
import sys

from github import GithubException
from github.Repository import Repository
from github.Team import Team

# 프로젝트 루트를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from scripts.github.common import (
    get_all_repos,
    get_github_client,
    get_organization,
)

VALID_PERMISSIONS = ["pull", "push", "admin", "maintain", "triage"]


def validate_permission(permission: str) -> str:
    """
    권한 문자열을 검증하는 함수

    Args:
        permission: 권한 문자열

    Returns:
        str: 검증된 권한 문자열

    Raises:
        ValueError: 유효하지 않은 권한인 경우
    """
    permission = permission.lower()
    if permission not in VALID_PERMISSIONS:
        raise ValueError(
            f"유효하지 않은 권한입니다. 허용된 값: {', '.join(VALID_PERMISSIONS)}"
        )
    return permission


def team_has_access(team: Team, repo: Repository) -> bool:
    """
    팀이 이미 리포지토리에 접근 권한이 있는지 확인하는 함수

    Args:
        team: PyGithub Team 객체
        repo: PyGithub Repository 객체

    Returns:
        bool: 이미 권한이 있으면 True
    """
    try:
        # 팀의 리포지토리 권한 확인
        team.get_repo_permission(repo)
        return True
    except GithubException as e:
        if e.status == 404:
            return False
        raise


def add_team_to_repo(team: Team, repo: Repository, permission: str) -> None:
    """
    팀을 리포지토리에 추가하는 함수

    Args:
        team: PyGithub Team 객체
        repo: PyGithub Repository 객체
        permission: 부여할 권한
    """
    team.add_to_repos(repo)
    team.set_repo_permission(repo, permission)


def main():
    parser = argparse.ArgumentParser(
        description="Organization의 모든 리포지토리에 특정 팀을 추가합니다."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 변경 없이 어떤 리포지토리에 팀이 추가될지 확인",
    )
    parser.add_argument(
        "--team",
        default="security",
        help="추가할 팀의 slug (기본값: security)",
    )
    parser.add_argument(
        "--permission",
        default="push",
        choices=VALID_PERMISSIONS,
        help="팀에 부여할 권한 (기본값: push)",
    )
    args = parser.parse_args()

    # 권한 검증
    permission = validate_permission(args.permission)

    # GitHub 클라이언트 초기화
    g = get_github_client()
    org = get_organization(g)

    # 팀 가져오기
    try:
        team = org.get_team_by_slug(args.team)
    except GithubException as e:
        if e.status == 404:
            print(f"[ERROR] 팀 '{args.team}'을 찾을 수 없습니다.")
            sys.exit(1)
        raise

    print(f"Organization: {org.login}")
    print(f"팀: {team.name} ({team.slug})")
    print(f"권한: {permission}")
    print("-" * 50)

    success_count = 0
    skip_count = 0
    error_count = 0

    for repo in get_all_repos(org):
        repo_name = repo.name
        try:
            if team_has_access(team, repo):
                print(f"[SKIP] {repo_name}: 팀 이미 추가됨")
                skip_count += 1
                continue

            if args.dry_run:
                print(f"[DRY-RUN] {repo_name}: 팀 추가 예정 (권한: {permission})")
                success_count += 1
            else:
                add_team_to_repo(team, repo, permission)
                print(f"[SUCCESS] {repo_name}: 팀 추가 완료 (권한: {permission})")
                success_count += 1

        except GithubException as e:
            print(f"[ERROR] {repo_name}: {e.data.get('message', str(e))}")
            error_count += 1

    print("-" * 50)
    print(f"완료: 성공 {success_count}, 스킵 {skip_count}, 오류 {error_count}")


if __name__ == "__main__":
    main()
