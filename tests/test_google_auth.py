"""
운영봇 서비스 계정 자격 증명 테스트
"""

import json
import pytest
from unittest.mock import patch

from api import google_auth

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
OPERATE_JSON = json.dumps({"type": "service_account", "project_id": "operate"})


@pytest.fixture
def from_info():
    with patch.object(google_auth.Credentials, "from_service_account_info") as m:
        yield m


def test_uses_operate_account(monkeypatch, from_info):
    monkeypatch.setenv(google_auth.ENV, OPERATE_JSON)

    google_auth.operate_credentials(SCOPES)

    assert from_info.call_args.args[0]["project_id"] == "operate"
    assert from_info.call_args.kwargs["scopes"] == SCOPES


def test_missing_env_fails_loudly(monkeypatch, from_info):
    """기존 시트 전용 계정으로 조용히 떨어지지 않고 즉시 실패해야 한다"""
    monkeypatch.delenv(google_auth.ENV, raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", OPERATE_JSON)

    with pytest.raises(KeyError):
        google_auth.operate_credentials(SCOPES)

    from_info.assert_not_called()
