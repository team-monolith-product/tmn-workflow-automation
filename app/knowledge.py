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

import asyncio
from typing import Any, Awaitable, Callable

from langchain_core.tools import tool
from slack_sdk.web.async_client import AsyncWebClient

from service.knowledge.db import connect
from service.knowledge.ingest import build_thread_row, upsert_item
from service.knowledge.register import (
    disable_source,
    upsert_source,
    validate_public_channel,
)
from service.knowledge.search import render_results, search_items
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


def _resolve_data_source_id(channel_id: str) -> int | None:
    """채널에 대응하는 data_source.id를 찾습니다.

    등록되지 않은 채널은 수집 대상이 아닙니다. 봇이 초대만 받고 아직
    data_source로 등록되지 않은 상태를 정상으로 취급합니다.

    Args:
        channel_id: Slack 채널 ID

    Returns:
        int | None: data_source.id. 미등록이면 None
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM data_source "
            "WHERE source = 'slack' AND external_id = %s AND enabled",
            (channel_id,),
        )
        row = cur.fetchone()
    return row["id"] if row else None


def _upsert_item(row) -> None:
    """스레드 행을 적재합니다. psycopg는 동기라 스레드에서 실행합니다."""
    with connect() as conn:
        upsert_item(conn, row)


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

    data_source_id = await asyncio.to_thread(_resolve_data_source_id, channel_id)
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
    await asyncio.to_thread(_upsert_item, row)


def get_knowledge_search_tools(client: AsyncWebClient, user_id: str | None) -> list:
    """사내 지식베이스를 검색하는 도구를 반환합니다.

    질의자를 도구 인자가 아니라 클로저로 받습니다. 에이전트가 남의 이름으로
    질의를 남기는 경로를 없애려는 것이고, query_log의 actor가 실제로 물어본
    사람을 가리켜야 무엇이 검색되지 않는지 추적할 수 있습니다.

    Args:
        client: 슬랙 클라이언트
        user_id: 질문자의 Slack 사용자 ID

    Returns:
        list: [검색 도구]
    """

    @tool
    async def search_knowledge(query: str, channel: str | None = None) -> str:
        """
        사내 슬랙 공개 채널의 과거 대화를 검색합니다.
        "예전에 이거 어떻게 했었지", "이 에러 본 적 있나" 같은 질문에 사용합니다.
        검색어는 어휘가 그대로 맞아야 하므로 문장이 아니라 핵심 단어를 넣습니다.
        channel에 "t_개발" 같은 채널 이름을 주면 그 채널로 좁힙니다.
        """
        emails = await fetch_user_emails(client)
        actor = emails.get(user_id, f"slack:{user_id}")
        return await asyncio.to_thread(_search_knowledge, query, actor, channel)

    return [search_knowledge]


def _search_knowledge(query: str, actor: str, channel: str | None) -> str:
    """지식베이스를 검색합니다. psycopg는 동기라 스레드에서 실행합니다."""
    with connect() as conn:
        results = search_items(conn, query, actor=actor, tool="slack", channel=channel)
    return render_results(results)


def get_knowledge_channel_tools(client: AsyncWebClient, channel_id: str) -> list:
    """멘션이 온 채널의 수집을 켜고 끄는 도구를 반환합니다.

    채널 ID를 도구 인자가 아니라 클로저로 받습니다. 멘션이 온 그 채널이
    대상이라는 걸 강제해서, 에이전트가 다른 채널 ID를 지어내 등록하는
    경로를 없앱니다.

    Args:
        client: 슬랙 클라이언트
        channel_id: 멘션이 온 채널 ID

    Returns:
        list: [수집 시작, 수집 중지] 도구
    """

    @tool
    async def enable_knowledge_collection() -> str:
        """
        이 채널을 지식베이스 수집 대상으로 등록합니다.
        "이 채널 지식 수집 시작해줘" 같은 요청에 사용합니다.
        """
        info = (await client.conversations_info(channel=channel_id))["channel"]
        rejection = validate_public_channel(info)
        if rejection:
            return rejection
        await asyncio.to_thread(_upsert_source, channel_id, info["name"])
        return (
            f"#{info['name']} 수집을 시작했습니다. 지금부터 오는 스레드가 적재됩니다."
        )

    @tool
    async def disable_knowledge_collection() -> str:
        """
        이 채널의 지식베이스 수집을 중지합니다.
        "이 채널 지식 수집 그만해줘" 같은 요청에 사용합니다.
        """
        data_source_id = await asyncio.to_thread(_disable_source, channel_id)
        if data_source_id is None:
            return "이 채널은 수집 대상이 아니었습니다."
        return "수집을 중지했습니다. 이미 적재된 스레드는 남아 있습니다."

    return [enable_knowledge_collection, disable_knowledge_collection]


def _upsert_source(channel_id: str, channel_name: str) -> None:
    """수집 대상으로 등록합니다. psycopg는 동기라 스레드에서 실행합니다."""
    with connect() as conn:
        upsert_source(conn, "slack", channel_id, channel_name)


def _disable_source(channel_id: str) -> int | None:
    """수집을 중지합니다. psycopg는 동기라 스레드에서 실행합니다."""
    with connect() as conn:
        return disable_source(conn, "slack", channel_id)


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
