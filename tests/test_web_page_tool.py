"""웹 조회의 네트워크 실패가 에이전트 대화를 중단하지 않는지 검증한다."""

import threading
from unittest.mock import patch

import pytest
from requests import Response
from requests.exceptions import ConnectTimeout, ConnectionError, ReadTimeout

from app.common import get_web_page_from_url


async def test_web_request_has_timeouts_and_runs_off_event_loop():
    """실제 로더의 HTTP 호출에 제한을 전달하고 문서 반환을 유지한다."""
    loop_thread = threading.get_ident()

    def get_page(url, **kwargs):
        assert threading.get_ident() != loop_thread
        assert url == "https://example.com"
        assert kwargs["timeout"] == (5, 30)
        response = Response()
        response.status_code = 200
        response._content = (
            b"<html><head><title>Example</title></head><body>hello</body></html>"
        )
        response.encoding = "utf-8"
        return response

    with patch("requests.Session.get", side_effect=get_page):
        documents = await get_web_page_from_url.ainvoke({"url": "https://example.com"})

    assert len(documents) == 1
    assert "hello" in documents[0].page_content
    assert documents[0].metadata["source"] == "https://example.com"


@pytest.mark.parametrize("error", [ConnectTimeout, ReadTimeout, ConnectionError])
async def test_network_failure_returns_guidance_to_agent(error):
    """연결/읽기 실패를 도구 결과로 반환해 LangGraph가 계속 진행하게 한다."""
    with patch("requests.Session.get", side_effect=error("private request details")):
        result = await get_web_page_from_url.ainvoke({"url": "https://example.com"})

    assert "불러올 수 없습니다" in result
    assert "다른 출처" in result
    assert "private request details" not in result


async def test_unexpected_loader_error_still_propagates():
    """네트워크 실패 외의 프로그래밍 오류를 감추지 않는다."""
    with patch("requests.Session.get", side_effect=ValueError("unexpected")):
        with pytest.raises(ValueError, match="unexpected"):
            await get_web_page_from_url.ainvoke({"url": "https://example.com"})
