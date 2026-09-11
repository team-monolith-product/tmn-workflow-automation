"""매출 MCP 도구·검증 스크립트 테스트. 시트는 읽지 않는다."""

from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from app.knowledge_mcp import build_mcp, build_mcp_app
from scripts.validate_revenue_ledger import format_alert
from scripts.validate_revenue_ledger import main as validate_main
from service.revenue import facts as facts_module
from tests.test_revenue_build import build

ADMIN = {"id": 7, "email": "lch@team-mono.com", "permissions": [], "tenants": []}
RESOURCE_URL = "https://wfa.codle.io"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Authorization": "Bearer valid-token",
}


@pytest.fixture
def mcp_env(monkeypatch):
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("MCP_RESOURCE_URL", RESOURCE_URL)


@pytest.fixture
def stub_facts(monkeypatch):
    """시트 대신 합성 facts 를 쓴다. 캐시도 비운다."""
    facts_module.clear_cache()
    facts = build()
    monkeypatch.setattr(facts_module, "build_fresh", lambda: facts)
    yield facts
    facts_module.clear_cache()


def call_tool(name: str, arguments: dict) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    mcp = build_mcp()
    with patch("app.mcp_common.get_me", AsyncMock(return_value=ADMIN)):
        with TestClient(build_mcp_app(mcp), base_url=RESOURCE_URL) as client:
            response = client.post("/mcp", json=payload, headers=HEADERS)
    assert response.status_code == 200, response.text
    # stateless HTTP 는 SSE 로 답한다. data: 줄 하나에 JSON-RPC 응답이 있다.
    line = next(l for l in response.text.splitlines() if l.startswith("data:"))
    import json

    body = json.loads(line[len("data:") :])
    assert "error" not in body, body
    return body["result"]["content"][0]["text"]


def test_요약_도구는_연도_집계를_돌려준다(mcp_env, stub_facts):
    text = call_tool("revenue_summary", {})

    assert "| 2026 | 1,500,000원" in text
    assert "2026년 상품 분류" in text


def test_거래_도구는_조건을_받는다(mcp_env, stub_facts):
    text = call_tool(
        "revenue_transactions", {"year": 2025, "customer": "호서", "status": "issued"}
    )

    assert "1건 1,000,000원" in text
    assert "호서고등학교" in text


def test_health_도구는_경고_없음을_말한다(mcp_env, stub_facts):
    assert "검증 경고 없음" in call_tool("revenue_health", {})


def test_캐시는_TTL_안에서_시트를_다시_읽지_않는다(monkeypatch):
    facts_module.clear_cache()
    calls = []
    monkeypatch.setattr(facts_module, "build_fresh", lambda: calls.append(1) or build())

    first = facts_module.get_facts()
    second = facts_module.get_facts()
    fresh = facts_module.get_facts(max_age_seconds=0)

    assert first is second
    assert fresh is not first
    assert len(calls) == 2
    facts_module.clear_cache()


def test_검증_스크립트는_경고_없으면_보내지_않는다(monkeypatch, capsys):
    monkeypatch.setattr("scripts.validate_revenue_ledger.build_fresh", build)
    posted = []
    monkeypatch.setattr(
        "scripts.validate_revenue_ledger.WebClient",
        lambda token: type(
            "C", (), {"chat_postMessage": lambda self, **kw: posted.append(kw)}
        )(),
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")

    validate_main()

    assert posted == []
    assert "검증 경고 없음" in capsys.readouterr().out


def test_검증_스크립트는_경고가_있으면_설정한_채널로_보낸다(monkeypatch):
    facts = build()
    facts["meta"]["problems"] = ["연도 총계 불일치 2025: 계산 1 vs 기준 2 (차 -1)"]
    monkeypatch.setattr("scripts.validate_revenue_ledger.build_fresh", lambda: facts)
    posted = []
    monkeypatch.setattr(
        "scripts.validate_revenue_ledger.WebClient",
        lambda token: type(
            "C", (), {"chat_postMessage": lambda self, **kw: posted.append(kw)}
        )(),
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")

    validate_main()

    assert len(posted) == 1
    assert posted[0]["channel"] == "C0BA4UXD2G7"
    assert "연도 총계 불일치 2025" in posted[0]["text"]


def test_경고_메시지는_상한을_넘기면_접는다():
    facts = build()
    facts["meta"]["problems"] = [f"경고 {i}" for i in range(20)]

    text = format_alert(facts)

    assert "검증 경고 20건" in text
    assert "경고 14" in text and "경고 15" not in text
    assert "외 5건" in text
