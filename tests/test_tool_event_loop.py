"""
데이터 봇 툴이 단일 이벤트 루프 위에서 도는지 검증하는 테스트

동기 툴(`@tool def`)을 하나라도 두면 langchain이 `invoke` 전체를 워커 스레드로 넘기고
그 안에서 콜백을 새 이벤트 루프로 돌린다. 그러면 콜백 핸들러가 들고 있는
`asyncio.Lock` 같은 루프 종속 객체가 깨진다.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.prebuilt import create_react_agent

from api import athena
from app.tools.athena_tools import execute_athena_query
from app.tools.redash_tools import (
    list_redash_dashboards,
    read_redash_dashboard,
    read_redash_query,
)


class LoopProbe(BaseCallbackHandler):
    """콜백이 실행된 이벤트 루프를 기록한다"""

    def __init__(self):
        self.loops = set()

    async def on_tool_start(self, serialized, input_str, **kwargs):
        self.loops.add(id(asyncio.get_running_loop()))

    async def on_tool_end(self, output, **kwargs):
        self.loops.add(id(asyncio.get_running_loop()))


class ToolCallingModel(FakeMessagesListChatModel):
    """create_react_agent가 요구하는 bind_tools만 채운 스텁 모델"""

    def bind_tools(self, tools, **kwargs):
        return self


TOOL_CALLS = [
    {"name": "list_redash_dashboards", "args": {}, "id": "c1", "type": "tool_call"},
    {
        "name": "read_redash_dashboard",
        "args": {"dashboard_id": 65},
        "id": "c2",
        "type": "tool_call",
    },
    {
        "name": "read_redash_query",
        "args": {"query_id": 1016},
        "id": "c3",
        "type": "tool_call",
    },
    {
        "name": "read_redash_query",
        "args": {"query_id": 1023},
        "id": "c4",
        "type": "tool_call",
    },
    {
        "name": "execute_athena_query",
        "args": {"query": "SELECT 1", "database": "jce_prd"},
        "id": "c5",
        "type": "tool_call",
    },
    {
        "name": "execute_athena_query",
        "args": {"query": "SELECT 2", "database": "jce_prd"},
        "id": "c6",
        "type": "tool_call",
    },
]


@patch("api.athena.execute_and_wait", new_callable=AsyncMock)
@patch("api.redash.get_query", new_callable=AsyncMock)
@patch("api.redash.get_dashboard", new_callable=AsyncMock)
@patch("api.redash.list_dashboards", new_callable=AsyncMock)
async def test_tool_callbacks_stay_on_one_event_loop(
    mock_list_dashboards, mock_get_dashboard, mock_get_query, mock_execute_and_wait
):
    """툴 여러 개를 동시에 호출해도 콜백은 전부 메인 루프에서 실행된다"""
    mock_list_dashboards.return_value = {"results": [{"id": 65, "name": "매출"}]}
    mock_get_dashboard.return_value = {"name": "매출", "widgets": []}
    mock_get_query.return_value = {"id": 1016, "name": "활성 교사", "query": "SELECT 1"}
    mock_execute_and_wait.return_value = {
        "ResultSet": {"Rows": [{"Data": [{"VarCharValue": "42"}]}]}
    }

    model = ToolCallingModel(
        responses=[
            AIMessage(content="", tool_calls=TOOL_CALLS),
            AIMessage(content="분석 결과입니다"),
        ]
    )
    agent = create_react_agent(
        model,
        [
            list_redash_dashboards,
            read_redash_dashboard,
            read_redash_query,
            execute_athena_query,
        ],
    )
    probe = LoopProbe()

    await agent.ainvoke(
        {"messages": [HumanMessage(content="지난 1년 활성 학교 알려줘")]},
        {"callbacks": [probe]},
    )

    assert probe.loops == {id(asyncio.get_running_loop())}


@patch("api.athena.get_query_status")
async def test_wait_for_query_completion_yields_to_loop(mock_get_query_status):
    """Athena 완료를 기다리는 동안 다른 코루틴이 진행된다"""
    mock_get_query_status.side_effect = [
        {"Status": {"State": "RUNNING"}},
        {"Status": {"State": "SUCCEEDED"}},
    ]
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    ticker = asyncio.create_task(tick())
    await athena.wait_for_query_completion("test-execution-id")
    ticker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ticker

    assert ticks > 0
