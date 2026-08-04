"""
공개 채널 메시지를 지식베이스에 적재하는 Slack 미들웨어입니다.

리스너(@app.event)가 아니라 미들웨어로 붙이는 이유가 있습니다. Bolt의 디스패치
루프는 리스너가 응답을 반환하면 거기서 순회를 멈춥니다. 대표 봇에는 이미
버그 채널을 처리하는 @app.event("message")가 있어서, 같은 이벤트에 리스너를
하나 더 달면 둘 중 하나만 실행됩니다.

next()를 먼저 호출하고 적재는 그 뒤에 합니다. 적재가 실패해도 버그 라우팅이
이미 끝나 있도록 하기 위해서입니다.

중복 이벤트는 걸러내지 않습니다. item의 (source, external_id) 유니크 제약과
content_hash 비교로 재전송이 무해하기 때문이고, app.event_dedup의 TTLCache는
전역 하나를 공유해서 기존 핸들러와 서로를 가로막습니다.
"""

from typing import Any, Awaitable, Callable

from slack_sdk.web.async_client import AsyncWebClient

from service.knowledge.db import connect
from service.knowledge.ingest import build_thread_row, upsert_thread
from service.knowledge.users import fetch_user_emails

SLACK_WORKSPACE_DOMAIN = "monolith-keb2010.slack.com"

# 스레드가 조용해진 뒤 정제한다. 답글마다 재정제하면 LLM 비용이 감당되지 않는다.
DISTILL_DELAY_SECONDS = 900

# 사람이 쓴 글이 아니거나 스레드를 다시 읽을 필요가 없는 이벤트
IGNORED_SUBTYPES = {
    "message_changed",
    "message_deleted",
    "channel_join",
    "channel_leave",
}


async def _resolve_data_source_id(conn, channel_id: str) -> int | None:
    """채널에 대응하는 data_source.id를 찾습니다.

    등록되지 않은 채널은 수집 대상이 아닙니다. 봇이 초대만 받고 아직
    data_source로 등록되지 않은 상태를 정상으로 취급합니다.

    Args:
        conn: 커넥션
        channel_id: Slack 채널 ID

    Returns:
        int | None: data_source.id. 미등록이면 None
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM data_source "
            "WHERE source = 'slack' AND external_id = %s AND enabled",
            (channel_id,),
        )
        row = cur.fetchone()
    return row["id"] if row else None


async def ingest_message_event(client: AsyncWebClient, body: dict[str, Any]) -> None:
    """메시지 이벤트가 속한 스레드를 통째로 다시 읽어 적재합니다.

    Args:
        client: Slack 클라이언트
        body: Slack 이벤트 body
    """
    event = body.get("event", {})
    if event.get("subtype") in IGNORED_SUBTYPES:
        return

    channel_id = event.get("channel")
    if not channel_id or event.get("channel_type") != "channel":
        return

    thread_ts = event.get("thread_ts") or event.get("ts")

    with connect() as conn:
        data_source_id = await _resolve_data_source_id(conn, channel_id)
        if data_source_id is None:
            return

        # 답글 하나가 와도 부모와 형제를 전부 다시 읽는다. 저장된 행이 항상
        # 완결된 대화를 반영해야 하기 때문이다.
        replies = await client.conversations_replies(channel=channel_id, ts=thread_ts)

        row = build_thread_row(
            data_source_id=data_source_id,
            channel_id=channel_id,
            messages=replies["messages"],
            workspace_domain=SLACK_WORKSPACE_DOMAIN,
            distill_delay_seconds=DISTILL_DELAY_SECONDS,
            user_emails=await fetch_user_emails(client),
        )
        upsert_thread(conn, row)


def register_knowledge_middleware(app) -> None:
    """지식베이스 적재 미들웨어를 등록합니다.

    Args:
        app: 대표 봇 AsyncApp
    """

    @app.middleware
    async def collect_public_channel_messages(
        body: dict[str, Any],
        client: AsyncWebClient,
        next: Callable[[], Awaitable[None]],
    ) -> None:
        # 기존 리스너를 먼저 끝낸다. 적재 실패가 버그 라우팅을 막지 않도록.
        await next()

        if body.get("event", {}).get("type") != "message":
            return

        await ingest_message_event(client, body)
