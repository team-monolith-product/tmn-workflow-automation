"""팀모노리스 로봇의 데이터 조회 도구를 MCP로 여는 배선 테스트"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from app.general import ROBOT_INSTRUCTIONS
from app.knowledge_mcp import build_mcp
from app.robot_mcp import robot_tools
from app.tools.athena_tools import is_read_only_query
from app.tools.data_guide import DATA_GUIDE

RESULTS = {
    "ResultSet": {
        "Rows": [
            {"Data": [{"VarCharValue": "n"}]},
            {"Data": [{"VarCharValue": "3"}]},
        ]
    }
}


@pytest.fixture
def mcp_env(monkeypatch):
    """build_mcp가 읽는 환경 변수를 채웁니다."""
    monkeypatch.setenv("ADMIN_RAILS_BASE_URL", "https://admin-rails.codle.io")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://wfa.codle.io")


async def test_redash와_읽기_전용_athena만_연다(mcp_env):
    tools = {t.name: t for t in await build_mcp().list_tools()}

    assert set(tools) == {
        "query_knowledge",
        "list_redash_dashboards",
        "read_redash_dashboard",
        "read_redash_query",
        "execute_athena_query",
    }
    # 결과를 슬랙에 올리는 스위치가 없다.
    athena = tools["execute_athena_query"].input_schema
    assert set(athena["properties"]) == {"query", "database"}
    assert athena["required"] == ["query", "database"]


async def test_입력_스키마는_로봇이_보는_것과_같다(mcp_env):
    """원래 함수를 등록해도 Field 로 단 인자 설명이 그대로 나갑니다."""
    served = {t.name: t.input_schema for t in await build_mcp().list_tools()}

    for lc_tool in robot_tools():
        expected = lc_tool.tool_call_schema.model_json_schema()
        assert served[lc_tool.name]["properties"] == expected["properties"]
        assert served[lc_tool.name].get("required", []) == expected.get("required", [])


async def test_athena는_조회_결과를_표로_돌려준다(mcp_env):
    with patch(
        "app.tools.athena_tools.athena.execute_and_wait",
        AsyncMock(return_value=RESULTS),
    ) as execute:
        result = await build_mcp().call_tool(
            "execute_athena_query",
            {"query": "SELECT 3 AS n", "database": "jce_prd"},
        )

    execute.assert_awaited_once_with("SELECT 3 AS n", database="jce_prd")
    assert "| n |" in result.content[0].text
    assert "3" in result.content[0].text


async def test_athena는_조회_문이_아니면_실행하지_않는다(mcp_env):
    with patch(
        "app.tools.athena_tools.athena.execute_and_wait", AsyncMock()
    ) as execute:
        result = await build_mcp().call_tool(
            "execute_athena_query",
            {"query": "DROP TABLE jce_prd.users", "database": "jce_prd"},
        )

    execute.assert_not_awaited()
    assert "조회 문(SELECT·WITH·SHOW·DESCRIBE·DESC·VALUES)" in result.content[0].text


@pytest.mark.parametrize(
    "query",
    [
        "select 1",
        "  -- 주석\nWITH a AS (SELECT 1) SELECT * FROM a",
        "/* 주석 */ (SELECT 1)",
        "( ( -- 주석\n /* 주석 */ select 1",
        "SHOW TABLES IN jce_prd '*class*'",
        "DESCRIBE jce_prd.users",
    ],
)
def test_조회_문은_통과한다(query):
    assert is_read_only_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "DROP TABLE t",
        "CREATE TABLE t AS SELECT 1",
        "INSERT INTO t SELECT 1",
        # EXPLAIN ANALYZE 는 문장을 실제로 실행한다.
        "EXPLAIN ANALYZE INSERT INTO t VALUES (1)",
        "UNLOAD (SELECT 1) TO 's3://x/'",
        "selected",
        "-- 주석뿐",
        "/* 닫히지 않은 주석 SELECT 1",
        "",
    ],
)
def test_쓰기_문과_빈_문장은_막는다(query):
    assert not is_read_only_query(query)


def test_주석이_겹친_긴_입력도_바로_판정한다():
    """정규식이면 백트래킹이 폭발하는 입력입니다(CodeQL py/redos)."""
    started = time.monotonic()

    assert not is_read_only_query("/*" + "*//*" * 200_000)
    assert time.monotonic() - started < 1


async def test_서버_안내에_DB_가이드가_있고_슬랙_전송_지침은_없다(mcp_env):
    instructions = build_mcp().instructions

    assert "Athena 데이터베이스 가이드" in instructions
    assert "show_result_to_user" not in instructions
    assert "upload_file" not in instructions


def test_슬랙_로봇의_지침은_DB_가이드로_시작한다():
    assert ROBOT_INSTRUCTIONS.startswith(DATA_GUIDE)
    assert "show_result_to_user=True" in ROBOT_INSTRUCTIONS
