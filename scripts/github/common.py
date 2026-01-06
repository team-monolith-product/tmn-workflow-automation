"""
GitHub API 공통 유틸리티 모듈

PyGithub 라이브러리를 사용하여 Organization의 리포지토리를 관리합니다.
보안을 위해 토큰은 환경변수로 관리됩니다.
"""
import os
import sys
from typing import Generator

from dotenv import load_dotenv
from github import Github, GithubException
from github.Organization import Organization
from github.Repository import Repository

# 프로젝트 루트의 .env 파일 로드
load_dotenv()


def get_github_client() -> Github:
    """
    GitHub 클라이언트를 생성하는 함수 (Organization 관리용)

    환경변수 GITHUB_ADMIN_TOKEN이 필요합니다.
    이 토큰은 Organization 관리 권한 (admin:org, write:org)이 필요합니다.

    Returns:
        Github: PyGithub 클라이언트 인스턴스

    Raises:
        ValueError: GITHUB_ADMIN_TOKEN 환경변수가 설정되지 않은 경우
    """
    token = os.getenv("GITHUB_ADMIN_TOKEN")
    if not token:
        raise ValueError(
            "GITHUB_ADMIN_TOKEN 환경변수가 설정되지 않았습니다. "
            "Organization 관리 권한이 있는 토큰을 .env 파일에 설정해주세요."
        )

    # 토큰 형식 기본 검증 (ghp_ 또는 github_pat_ 접두사)
    if not (token.startswith("ghp_") or token.startswith("github_pat_")):
        print(
            "경고: GitHub 토큰 형식이 예상과 다릅니다. "
            "Personal Access Token 또는 Fine-grained Token인지 확인해주세요.",
            file=sys.stderr,
        )

    return Github(token, timeout=30, retry=3)


def get_org_name() -> str:
    """
    Organization 이름을 환경변수에서 가져오는 함수

    Returns:
        str: Organization 이름

    Raises:
        ValueError: GITHUB_ORG_NAME 환경변수가 설정되지 않은 경우
    """
    org_name = os.getenv("GITHUB_ORG_NAME")
    if not org_name:
        raise ValueError(
            "GITHUB_ORG_NAME 환경변수가 설정되지 않았습니다. "
            ".env 파일 또는 환경변수를 확인해주세요."
        )
    return org_name


def get_organization(g: Github, org_name: str | None = None) -> Organization:
    """
    GitHub Organization 객체를 가져오는 함수

    Args:
        g: PyGithub 클라이언트
        org_name: Organization 이름 (None이면 환경변수에서 가져옴)

    Returns:
        Organization: PyGithub Organization 객체

    Raises:
        GithubException: Organization을 찾을 수 없는 경우
    """
    if org_name is None:
        org_name = get_org_name()

    try:
        return g.get_organization(org_name)
    except GithubException as e:
        if e.status == 404:
            raise ValueError(f"Organization '{org_name}'을 찾을 수 없습니다.") from e
        raise


def get_all_repos(
    org: Organization, include_forks: bool = False, include_archived: bool = False
) -> Generator[Repository, None, None]:
    """
    Organization의 모든 리포지토리를 가져오는 제너레이터 함수

    Args:
        org: PyGithub Organization 객체
        include_forks: Fork된 리포지토리 포함 여부 (기본값: False)
        include_archived: Archive된 리포지토리 포함 여부 (기본값: False)

    Yields:
        Repository: 필터링 조건을 만족하는 리포지토리
    """
    for repo in org.get_repos(type="all"):
        # Fork 리포지토리 필터링
        if not include_forks and repo.fork:
            continue

        # Archive된 리포지토리 필터링
        if not include_archived and repo.archived:
            continue

        yield repo


def validate_repo_name(repo_name: str) -> bool:
    """
    리포지토리 이름이 유효한지 검증하는 함수

    Args:
        repo_name: 검증할 리포지토리 이름

    Returns:
        bool: 유효한 경우 True
    """
    if not repo_name:
        return False

    # GitHub 리포지토리 이름 규칙: 영문자, 숫자, -, _, . 만 허용
    import re

    pattern = r"^[a-zA-Z0-9._-]+$"
    return bool(re.match(pattern, repo_name))
