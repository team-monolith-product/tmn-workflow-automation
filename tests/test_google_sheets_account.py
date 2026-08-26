"""서비스 계정 분리 테스트.

계정이 곧 **볼 수 있는 시트의 범위**다. 섞이면 한쪽 용도의 공유 범위가 다른
쪽에 그대로 열리고, 그 사실이 코드 어디에도 안 보인다.
"""

import pytest

from api import google_sheets

KEY = '{"type":"service_account"}'


class FakeClient:
    def set_timeout(self, seconds):
        pass


class FakeDrive:
    """list_spreadsheet_files 가 쓰는 것은 http_client.request 하나뿐이다."""

    class http_client:
        @staticmethod
        def request(method, url, params=None):
            return type("Response", (), {"json": staticmethod(lambda: {"files": []})})


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    """자격증명 생성을 걷어내고 캐시를 비웁니다."""
    google_sheets._clients.clear()
    monkeypatch.setattr(
        google_sheets.Credentials,
        "from_service_account_info",
        staticmethod(lambda info, scopes: info),
    )
    monkeypatch.setattr(google_sheets.gspread, "authorize", lambda c: FakeClient())
    yield
    google_sheets._clients.clear()


def test_계정마다_클라이언트가_따로다(monkeypatch):
    monkeypatch.setenv(google_sheets.DEFAULT_ACCOUNT, KEY)
    monkeypatch.setenv(google_sheets.OPERATING_ACCOUNT, KEY)

    기본 = google_sheets._get_client()
    운영 = google_sheets._get_client(google_sheets.OPERATING_ACCOUNT)

    assert 기본 is not 운영
    assert set(google_sheets._clients) == {
        google_sheets.DEFAULT_ACCOUNT,
        google_sheets.OPERATING_ACCOUNT,
    }


def test_같은_계정은_한_번만_만든다(monkeypatch):
    monkeypatch.setenv(google_sheets.DEFAULT_ACCOUNT, KEY)

    assert google_sheets._get_client() is google_sheets._get_client()


def test_환경변수가_없으면_폴백하지_않고_터진다(monkeypatch):
    # .get() 으로 바꿔 기본 계정으로 흘려보내면 "왜 그 시트가 안 보이지" 를
    # 며칠 뒤에 겪는다.
    monkeypatch.delenv(google_sheets.OPERATING_ACCOUNT, raising=False)

    with pytest.raises(KeyError, match=google_sheets.OPERATING_ACCOUNT):
        google_sheets._get_client(google_sheets.OPERATING_ACCOUNT)


def test_목록_조회는_운영_계정을_쓴다(monkeypatch):
    # 기본값으로 나가면 Drive API 가 안 켜진 프로젝트라 403 이 난다.
    쓴계정 = []
    monkeypatch.setattr(
        google_sheets,
        "_get_client",
        lambda account=None: 쓴계정.append(account) or FakeDrive(),
    )

    google_sheets.list_spreadsheet_files()

    assert 쓴계정 == [google_sheets.OPERATING_ACCOUNT]
