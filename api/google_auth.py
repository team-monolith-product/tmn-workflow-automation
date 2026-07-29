"""
Google 서비스 계정 자격 증명 로더
"""

import json
import os

from google.oauth2.service_account import Credentials

# 운영봇 전용 서비스 계정. 없으면 공용 계정으로 떨어진다.
OPERATE_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON_OPERATE"
SHARED_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"


def operate_credentials(scopes: list[str]) -> Credentials:
    """
    운영봇용 자격 증명을 만든다.

    운영봇은 Drive 쓰기 권한과 공유 드라이브 접근이 필요해 공용 계정보다 권한이 넓다.
    계정을 분리해 두면 봇에 준 권한이 기존 스크립트로 번지지 않고, 문제가 생겼을 때
    봇 계정만 회수할 수 있다.

    GOOGLE_SERVICE_ACCOUNT_JSON_OPERATE가 없으면 공용 계정을 쓴다. 계정을 새로 만들기
    전에도 동작하게 하려는 것이며, 이때는 공용 계정에 Drive 권한이 필요하다.
    """
    sa_json = os.environ.get(OPERATE_ENV) or os.environ[SHARED_ENV]
    return Credentials.from_service_account_info(json.loads(sa_json), scopes=scopes)


def shared_credentials(scopes: list[str]) -> Credentials:
    """공용 서비스 계정 자격 증명을 만든다."""
    return Credentials.from_service_account_info(
        json.loads(os.environ[SHARED_ENV]), scopes=scopes
    )
