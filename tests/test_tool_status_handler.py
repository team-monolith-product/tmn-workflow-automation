"""긴 도구 이력에서도 Slack 진행 메시지를 계속 갱신할 수 있는지 검증한다."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.tool_status_handler import ToolStatusHandler


def make_handler():
    return ToolStatusHandler(
        say=AsyncMock(return_value={"ts": "123.456"}),
        thread_ts="123.000",
        slack_client=AsyncMock(),
        channel="test-channel",
    )


def history(count, status="completed"):
    return [
        {
            "run_id": f"run-{i}",
            "name": f"read_query_{i}",
            "params": "긴 쿼리 내용 " * 20,
            "status": status,
        }
        for i in range(count)
    ]


def test_short_history_keeps_tool_details():
    handler = make_handler()
    handler.tool_history = history(1)
    text = handler._format_status_text()
    assert "✅ read_query_0(" in text
    assert len(text) <= 3000


@pytest.mark.parametrize("status", ["completed", "running", "error"])
def test_long_history_is_bounded_and_shows_counts(monkeypatch, status):
    monkeypatch.setenv("LANGSMITH_ORG_ID", str(uuid4()))
    monkeypatch.setenv("LANGSMITH_PROJECT_ID", str(uuid4()))
    handler = make_handler()
    handler.langsmith_run_id = uuid4()
    handler.tool_history = history(200, status)
    handler.tool_history.append(
        {
            "run_id": "latest",
            "name": "current_tool",
            "params": "{}",
            "status": "running",
        }
    )

    text = handler._format_status_text()

    assert len(text) <= 3000
    if status == "running":
        assert "⏳ 실행 중 201개" in text
    else:
        assert "200개" in text
    assert "⏳ current_tool({})" in text
    assert "LangSmith Trace>" in text
    assert "이전 호출 상세 생략" in text


async def test_all_callbacks_send_valid_blocks_after_history_grows():
    """시작·완료·오류 콜백의 실제 Slack payload를 검증한다."""
    handler = make_handler()
    handler.tool_history = history(200)

    def validate_blocks(*args, **kwargs):
        payload = args[0] if args else kwargs
        for block in payload["blocks"]:
            assert 1 <= len(block["text"]["text"]) <= 3000
        return {"ts": "123.456"}

    handler.say.side_effect = validate_blocks
    handler.slack_client.chat_update.side_effect = validate_blocks
    await handler.on_tool_start({"name": "new_tool"}, "{}", run_id="new")
    await handler.on_tool_end("done", run_id="new")
    await handler.on_tool_start({"name": "failed_tool"}, "{}", run_id="failed")
    await handler.on_tool_error(RuntimeError("failed"), run_id="failed")

    assert handler.say.await_count == 1
    assert handler.slack_client.chat_update.await_count == 3
    assert handler.tool_history[-1]["status"] == "error"
