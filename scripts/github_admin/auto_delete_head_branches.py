"""
GitHub Organization의 모든 리포지토리에 delete_branch_on_merge 설정을 적용하는 스크립트

PR 병합 시 head 브랜치를 자동으로 삭제하도록 설정합니다.

사용법:
    python scripts/github_admin/auto_delete_head_branches.py [--dry-run]

옵션:
    --dry-run: 실제 변경 없이 어떤 리포지토리에 설정이 적용될지 확인
"""

import argparse
import os
import sys

from github import GithubException
from github.Repository import Repository

# 프로젝트 루트를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from scripts.github_admin.common import (
    get_all_repos,
    get_github_client,
    get_organization,
)


def update_delete_branch_on_merge(repo: Repository, enable: bool = True) -> bool:
    """
    리포지토리의 delete_branch_on_merge 설정을 업데이트하는 함수

    Args:
        repo: PyGithub Repository 객체
        enable: 활성화 여부 (기본값: True)

    Returns:
        bool: 변경이 필요했으면 True, 이미 설정되어 있으면 False
    """
    # 현재 설정 확인
    if repo.delete_branch_on_merge == enable:
        return False

    # 설정 업데이트
    repo.edit(delete_branch_on_merge=enable)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Organization의 모든 리포지토리에 delete_branch_on_merge 설정을 적용합니다."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 변경 없이 어떤 리포지토리에 설정이 적용될지 확인",
    )
    args = parser.parse_args()

    # GitHub 클라이언트 초기화
    g = get_github_client()
    org = get_organization(g)

    print(f"Organization: {org.login}")
    print("설정: delete_branch_on_merge = True")
    print("-" * 50)

    success_count = 0
    skip_count = 0
    error_count = 0

    for repo in get_all_repos(org):
        repo_name = repo.name
        try:
            # 현재 설정 확인
            if repo.delete_branch_on_merge:
                print(f"[SKIP] {repo_name}: 이미 설정됨")
                skip_count += 1
                continue

            if args.dry_run:
                print(f"[DRY-RUN] {repo_name}: 설정 적용 예정")
                success_count += 1
            else:
                update_delete_branch_on_merge(repo, enable=True)
                print(f"[SUCCESS] {repo_name}: 설정 적용 완료")
                success_count += 1

        except GithubException as e:
            print(f"[ERROR] {repo_name}: {e.data.get('message', str(e))}")
            error_count += 1

    print("-" * 50)
    print(f"완료: 성공 {success_count}, 스킵 {skip_count}, 오류 {error_count}")


if __name__ == "__main__":
    main()
