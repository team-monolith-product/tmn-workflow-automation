"""
서비스 계정 자격 증명 로더 테스트
"""

import json
import pytest
from unittest.mock import patch

from api import google_auth

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
OPERATE_JSON = json.dumps({"type": "service_account", "project_id": "operate"})
SHARED_JSON = json.dumps({"type": "service_account", "project_id": "shared"})


@pytest.fixture
def from_info():
    with patch.object(google_auth.Credentials, "from_service_account_info") as m:
        yield m


class TestOperateCredentials:
    def test_prefers_operate_account(self, monkeypatch, from_info):
        """운영봇 전용 계정이 있으면 그것을 쓴다"""
        monkeypatch.setenv(google_auth.OPERATE_ENV, OPERATE_JSON)
        monkeypatch.setenv(google_auth.SHARED_ENV, SHARED_JSON)

        google_auth.operate_credentials(SCOPES)

        assert from_info.call_args.args[0]["project_id"] == "operate"

    def test_falls_back_to_shared_account(self, monkeypatch, from_info):
        """전용 계정이 없으면 공용 계정으로 떨어진다"""
        monkeypatch.delenv(google_auth.OPERATE_ENV, raising=False)
        monkeypatch.setenv(google_auth.SHARED_ENV, SHARED_JSON)

        google_auth.operate_credentials(SCOPES)

        assert from_info.call_args.args[0]["project_id"] == "shared"

    def test_empty_operate_value_falls_back(self, monkeypatch, from_info):
        """빈 문자열도 미설정으로 취급한다"""
        monkeypatch.setenv(google_auth.OPERATE_ENV, "")
        monkeypatch.setenv(google_auth.SHARED_ENV, SHARED_JSON)

        google_auth.operate_credentials(SCOPES)

        assert from_info.call_args.args[0]["project_id"] == "shared"

    def test_scopes_are_passed_through(self, monkeypatch, from_info):
        monkeypatch.setenv(google_auth.OPERATE_ENV, OPERATE_JSON)

        google_auth.operate_credentials(SCOPES)

        assert from_info.call_args.kwargs["scopes"] == SCOPES
