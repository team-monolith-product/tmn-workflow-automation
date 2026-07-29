"""
운영봇 서비스 계정 자격 증명

Drive와 Sheets가 같은 계정을 써야 한다. 작업 공간 폴더를 운영봇 계정에 공유하므로,
그 폴더 안의 스프레드시트도 같은 계정으로 읽어야 접근이 된다.
소비 방식은 다르다 — Drive는 build(credentials=...), Sheets는 gspread.authorize().
"""

import json
import os

from google.oauth2.service_account import Credentials

# 운영봇 전용 계정. 없으면 공용 계정으로 떨어진다.
OPERATE_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON_OPERATE"
SHARED_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"


def operate_credentials(scopes: list[str]) -> Credentials:
    """
    운영봇용 자격 증명을 만든다.

    GOOGLE_SERVICE_ACCOUNT_JSON_OPERATE가 없으면 공용 계정을 쓴다. 계정을 새로 만들기
    전에도 동작하게 하려는 것이며, 이때는 공용 계정에 Drive 읽기 권한이 필요하다.
    """
    sa_json = os.environ.get(OPERATE_ENV) or os.environ[SHARED_ENV]
    return Credentials.from_service_account_info(json.loads(sa_json), scopes=scopes)
