"""
AsyncImmediateAckSocketModeHandler 테스트

events_api에 대해 즉시 ack를 전송하고,
그 외 타입은 기존 동작을 유지하는지 검증합니다.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.socket_mode_handler import (
    AsyncImmediateAckSocketModeHandler,
    _background_tasks,
)


def _make_request(req_type: str = "events_api", envelope_id: str = "env-123"):
    req = MagicMock()
    req.type = req_type
    req.envelope_id = envelope_id
    req.payload = {"event": {"type": "app_mention"}}
    return req


@pytest.fixture
def mock_client():
    client = AsyncMock()
    return client


@pytest.fixture
def handler():
    app = MagicMock()
    h = AsyncImmediateAckSocketModeHandler.__new__(
        AsyncImmediateAckSocketModeHandler
    )
    h.app = app
    return h


@pytest.mark.asyncio
async def test_events_api_acks_immediately(handler, mock_client):
    """events_api 요청은 핸들러 실행 전에 즉시 ack를 전송합니다."""
    req = _make_request("events_api")
    ack_sent = asyncio.Event()
    dispatch_started = asyncio.Event()
    dispatch_can_finish = asyncio.Event()

    original_send = mock_client.send_socket_mode_response

    async def track_ack(response):
        ack_sent.set()
        return await original_send(response)

    mock_client.send_socket_mode_response = AsyncMock(side_effect=track_ack)

    async def slow_dispatch(app, r):
        dispatch_started.set()
        await dispatch_can_finish.wait()
        return MagicMock(status=200, body="", headers={})

    with patch(
        "app.socket_mode_handler.run_async_bolt_app", side_effect=slow_dispatch
    ):
        await handler.handle(mock_client, req)

        # handle()이 반환된 시점에서 이미 ack가 전송되어야 함
        assert ack_sent.is_set(), "ack should be sent before handle() returns"

        # 백그라운드 태스크가 실행 중인지 확인
        await asyncio.sleep(0.01)
        assert dispatch_started.is_set(), "dispatch should have started in background"

        # 백그라운드 태스크 완료 허용
        dispatch_can_finish.set()
        await asyncio.sleep(0.01)

    mock_client.send_socket_mode_response.assert_called_once()
    response = mock_client.send_socket_mode_response.call_args[0][0]
    assert response.envelope_id == "env-123"


@pytest.mark.asyncio
async def test_slash_commands_use_normal_flow(handler, mock_client):
    """slash_commands는 기존 동작을 유지합니다 (핸들러 완료 후 ack)."""
    req = _make_request("slash_commands")

    bolt_resp = MagicMock(status=200, body="", headers={})

    with patch(
        "app.socket_mode_handler.run_async_bolt_app",
        new_callable=AsyncMock,
        return_value=bolt_resp,
    ), patch(
        "app.socket_mode_handler.send_async_response",
        new_callable=AsyncMock,
    ) as mock_send:
        await handler.handle(mock_client, req)

    mock_send.assert_called_once()
    # send_async_response가 client, req, bolt_resp, start_time으로 호출됨
    call_args = mock_send.call_args[0]
    assert call_args[0] is mock_client
    assert call_args[1] is req
    assert call_args[2] is bolt_resp


@pytest.mark.asyncio
async def test_interactive_uses_normal_flow(handler, mock_client):
    """interactive 요청도 기존 동작을 유지합니다."""
    req = _make_request("interactive")

    bolt_resp = MagicMock(status=200, body='{"text": "ok"}', headers={"content-type": ["application/json"]})

    with patch(
        "app.socket_mode_handler.run_async_bolt_app",
        new_callable=AsyncMock,
        return_value=bolt_resp,
    ), patch(
        "app.socket_mode_handler.send_async_response",
        new_callable=AsyncMock,
    ) as mock_send:
        await handler.handle(mock_client, req)

    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_events_api_dispatch_error_is_logged(handler, mock_client):
    """events_api 처리 중 에러가 발생해도 ack는 이미 전송되고, 에러는 로깅됩니다."""
    req = _make_request("events_api")

    async def failing_dispatch(app, r):
        raise RuntimeError("LLM call failed")

    with patch(
        "app.socket_mode_handler.run_async_bolt_app", side_effect=failing_dispatch
    ), patch("app.socket_mode_handler.logger") as mock_logger:
        await handler.handle(mock_client, req)
        # 백그라운드 태스크 완료 대기
        await asyncio.sleep(0.05)

    # ack는 전송됨
    mock_client.send_socket_mode_response.assert_called_once()
    # 에러가 로깅됨
    mock_logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_background_tasks_tracked(handler, mock_client):
    """백그라운드 태스크가 _background_tasks set에 추가되고 완료 후 제거됩니다."""
    req = _make_request("events_api")
    initial_count = len(_background_tasks)

    finish_event = asyncio.Event()

    async def slow_dispatch(app, r):
        await finish_event.wait()
        return MagicMock(status=200, body="", headers={})

    with patch(
        "app.socket_mode_handler.run_async_bolt_app", side_effect=slow_dispatch
    ):
        await handler.handle(mock_client, req)
        await asyncio.sleep(0.01)

        # 태스크가 실행 중이면 set에 있어야 함
        assert len(_background_tasks) > initial_count

        finish_event.set()
        await asyncio.sleep(0.05)

    # 완료 후 제거됨
    assert len(_background_tasks) == initial_count
