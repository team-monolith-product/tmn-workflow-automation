"""
운영봇 서비스 계정 자격 증명

기존 GOOGLE_SERVICE_ACCOUNT_JSON은 특정 스프레드시트에만 권한이 있는 별도 계정이므로
운영봇이 쓸 수 없다. 폴백을 두면 Drive 권한이 없는 계정으로 조용히 붙어 실패하므로
운영봇 계정을 필수로 요구한다.

Drive와 Sheets가 같은 계정을 써야 한다. 작업 공간 폴더를 운영봇 계정에 공유하므로,
그 폴더 안의 스프레드시트도 같은 계정으로 읽어야 접근이 된다.
소비 방식은 다르다 — Drive는 build(credentials=...), Sheets는 gspread.authorize().
"""

import json
import os

from google.oauth2.service_account import Credentials

ENV = "GOOGLE_SERVICE_ACCOUNT_JSON_OPERATE"


def operate_credentials(scopes: list[str]) -> Credentials:
    """운영봇용 자격 증명을 만든다. 환경 변수가 없으면 KeyError로 즉시 실패한다."""
    return Credentials.from_service_account_info(
        json.loads(os.environ[ENV]), scopes=scopes
    )
