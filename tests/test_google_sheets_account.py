"""서비스 계정 분리 테스트.

계정이 곧 **볼 수 있는 시트의 범위**다. 섞이면 한쪽 용도의 공유 범위가 다른
쪽에 그대로 열리고, 그 사실이 코드 어디에도 안 보인다.
"""

import json

import pytest

from api import google_sheets


@pytest.fixture(autouse=True)
def _clear_clients():
    """계정별 클라이언트 캐시를 테스트 사이에 비웁니다."""
    google_sheets._clients.clear()
    yield
    google_sheets._clients.clear()


def _fake_key(project: str) -> str:
    return json.dumps({"type": "service_account", "project_id": project})


@pytest.fixture
def made(monkeypatch):
    """authorize 를 가로채 어떤 자격증명으로 만들었는지 기록합니다."""
    seen = []

    class FakeClient:
        def __init__(self, project):
            self.project = project

        def set_timeout(self, seconds):
            self.timeout = seconds

    def authorize(credentials, **kwargs):
        seen.append(credentials)
        return FakeClient(credentials)

    monkeypatch.setattr(
        google_sheets.Credentials,
        "from_service_account_info",
        staticmethod(lambda info, scopes: info["project_id"]),
    )
    monkeypatch.setattr(google_sheets.gspread, "authorize", authorize)
    return seen


def test_계정마다_클라이언트가_따로다(monkeypatch, made):
    monkeypatch.setenv(google_sheets.DEFAULT_ACCOUNT, _fake_key("디스코드용"))
    monkeypatch.setenv(google_sheets.OPERATING_ACCOUNT, _fake_key("운영시트용"))

    기본 = google_sheets._get_client()
    카탈로그 = google_sheets._get_client(google_sheets.OPERATING_ACCOUNT)

    assert 기본.project == "디스코드용"
    assert 카탈로그.project == "운영시트용"
    assert 기본 is not 카탈로그


def test_같은_계정은_한_번만_만든다(monkeypatch, made):
    monkeypatch.setenv(google_sheets.DEFAULT_ACCOUNT, _fake_key("디스코드용"))

    first = google_sheets._get_client()
    second = google_sheets._get_client()

    assert first is second
    assert len(made) == 1


def test_기본값은_원래_쓰던_계정이다():
    # 기존 호출부(discord_post_completion_notice)가 코드 변경 없이 그대로 돌아야
    # 한다. 기본값이 바뀌면 그 스크립트가 조용히 다른 계정으로 읽는다.
    import inspect

    default = (
        inspect.signature(google_sheets.get_worksheet_values)
        .parameters["account"]
        .default
    )

    assert default == google_sheets.DEFAULT_ACCOUNT == "GOOGLE_SERVICE_ACCOUNT_JSON"


def test_환경변수가_없으면_폴백하지_않고_터진다(monkeypatch, made):
    # 조용히 다른 계정으로 돌면 "왜 그 시트가 안 보이지" 를 며칠 뒤에 겪는다.
    monkeypatch.setenv(google_sheets.DEFAULT_ACCOUNT, _fake_key("디스코드용"))
    monkeypatch.delenv(google_sheets.OPERATING_ACCOUNT, raising=False)

    with pytest.raises(KeyError, match="OPERATING_SHEET_SERVICE_ACCOUNT_JSON"):
        google_sheets._get_client(google_sheets.OPERATING_ACCOUNT)


def test_카탈로그_경로는_운영시트_계정을_쓴다(monkeypatch):
    # list_spreadsheet_files·get_worksheet_headers 는 계정이 고정이다.
    쓴계정 = []

    class FakeHTTP:
        def fetch_sheet_metadata(self, spreadsheet_id, params=None):
            return {"sheets": [{"properties": {"sheetId": 0, "title": "명단"}}]}

        def values_batch_get(self, spreadsheet_id, ranges, params=None):
            return {"valueRanges": [{"values": [["성함"]]}]}

    class FakeClient:
        http_client = FakeHTTP()

    def fake_get_client(account=google_sheets.DEFAULT_ACCOUNT):
        쓴계정.append(account)
        return FakeClient()

    monkeypatch.setattr(google_sheets, "_get_client", fake_get_client)

    google_sheets.get_worksheet_headers("X")

    assert 쓴계정 == [google_sheets.OPERATING_ACCOUNT]
