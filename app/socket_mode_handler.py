"""
즉시 envelope ack를 전송하는 Socket Mode 핸들러

기본 AsyncSocketModeHandler는 이벤트 핸들러 완료 후 ack를 전송하므로,
LLM 호출 등 오래 걸리는 핸들러에서 Slack이 ~20초 후 이벤트를 재전송합니다.
events_api 타입만 ack를 먼저 전송하고 처리를 백그라운드로 위임합니다.
"""

import asyncio
import logging
from time import time

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.adapter.socket_mode.async_internals import (
    run_async_bolt_app,
    send_async_response,
)
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

logger = logging.getLogger(__name__)

# create_task 참조를 유지하여 GC 방지
_background_tasks: set[asyncio.Task] = set()


class AsyncImmediateAckSocketModeHandler(AsyncSocketModeHandler):
    """events_api에 대해 envelope ack를 즉시 전송하는 Socket Mode 핸들러."""

    async def handle(
        self, client: AsyncBaseSocketModeClient, req: SocketModeRequest
    ) -> None:
        if req.type == "events_api":
            # events_api는 ack 페이로드가 불필요하므로 즉시 ack 전송
            await client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id)
            )
            # 리스너 루프를 블로킹하지 않도록 백그라운드 태스크로 실행
            task = asyncio.create_task(self._dispatch_event(req))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        else:
            # slash_commands, interactive 등은 ack에 응답 페이로드가 필요하므로
            # 기존 동작 유지 (핸들러 완료 후 ack)
            start = time()
            bolt_resp = await run_async_bolt_app(self.app, req)
            await send_async_response(client, req, bolt_resp, start)

    async def _dispatch_event(self, req: SocketModeRequest) -> None:
        try:
            await run_async_bolt_app(self.app, req)
        except Exception:
            logger.exception("Failed to handle Socket Mode event")
