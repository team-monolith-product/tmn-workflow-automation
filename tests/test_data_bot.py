"""
데이터 분석 Bot 관련 모듈들의 단위 테스트
"""

import asyncio
import json

import pytest
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request.async_request import AsyncBoltRequest
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from api import athena, redash
from app import data_bot
from app.tools import athena_tools, redash_tools


class TestAthenaAPI:
    """AWS Athena API 래퍼 함수 테스트"""

    def test_get_athena_client(self):
        """Athena 클라이언트 생성 테스트"""
        client = athena.get_athena_client()
        assert client is not None
        assert hasattr(client, "start_query_execution")

    @patch("api.athena.get_athena_client")
    def test_execute_query(self, mock_get_client):
        """쿼리 실행 테스트"""
        mock_client = Mock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "test-execution-id"
        }
        mock_get_client.return_value = mock_client

        query = "SELECT 1"
        database = "test_db"
        execution_id = athena.execute_query(query, database)

        assert execution_id == "test-execution-id"
        mock_client.start_query_execution.assert_called_once()

    @patch("api.athena.get_athena_client")
    def test_get_query_status(self, mock_get_client):
        """쿼리 상태 조회 테스트"""
        mock_client = Mock()
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "QueryExecutionId": "test-id",
            }
        }
        mock_get_client.return_value = mock_client

        status = athena.get_query_status("test-id")

        assert status["Status"]["State"] == "SUCCEEDED"
        mock_client.get_query_execution.assert_called_once_with(
            QueryExecutionId="test-id"
        )


class TestRedashAPI:
    """Redash API 래퍼 함수 테스트"""

    def test_get_base_url(self):
        """Redash 기본 URL 가져오기 테스트"""
        url = redash.get_base_url()
        assert isinstance(url, str)

    def test_get_api_key(self):
        """Redash API 키 가져오기 테스트"""
        api_key = redash.get_api_key()
        assert isinstance(api_key, str)

    def test_get_headers(self):
        """Redash API 헤더 생성 테스트"""
        headers = redash.get_headers()
        assert "Authorization" in headers
        assert "Content-Type" in headers
        assert headers["Content-Type"] == "application/json"

    @patch("api.redash.requests.get")
    def test_list_dashboards(self, mock_get):
        """대시보드 목록 조회 테스트"""
        mock_response = Mock()
        mock_response.json.return_value = {
            "results": [{"name": "Test Dashboard", "slug": "test-dashboard"}]
        }
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = redash.list_dashboards(query="test")

        assert "results" in result
        assert len(result["results"]) == 1
        mock_get.assert_called_once()

    @patch("api.redash.requests.get")
    def test_get_query(self, mock_get):
        """쿼리 조회 테스트"""
        mock_response = Mock()
        mock_response.json.return_value = {
            "id": 123,
            "name": "Test Query",
            "query": "SELECT 1",
        }
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        result = redash.get_query(123)

        assert result["id"] == 123
        assert result["name"] == "Test Query"


class TestAthenaTools:
    """Athena LangChain Tools 테스트"""

    def test_format_query_results_empty(self):
        """빈 결과 포맷팅 테스트"""
        results = {}
        formatted = athena_tools.format_query_results_as_markdown(results)
        assert formatted == "결과가 없습니다."

    def test_format_query_results_with_data(self):
        """데이터가 있는 결과 포맷팅 테스트"""
        results = {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "id"}, {"VarCharValue": "name"}]},
                    {"Data": [{"VarCharValue": "1"}, {"VarCharValue": "Alice"}]},
                    {"Data": [{"VarCharValue": "2"}, {"VarCharValue": "Bob"}]},
                ]
            }
        }
        formatted = athena_tools.format_query_results_as_markdown(results)

        assert "id" in formatted
        assert "name" in formatted
        assert "Alice" in formatted
        assert "Bob" in formatted
        assert "|" in formatted  # 마크다운 테이블 형식

    @pytest.mark.asyncio
    @patch("api.athena.execute_and_wait")
    async def test_execute_athena_query_tool(self, mock_execute):
        """Athena 쿼리 실행 tool 테스트"""
        mock_execute.return_value = {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "count"}]},
                    {"Data": [{"VarCharValue": "42"}]},
                ]
            }
        }

        result = await athena_tools.execute_athena_query.ainvoke(
            {"query": "SELECT COUNT(*) as count FROM test", "database": "test_db"}
        )

        assert "count" in result
        assert "42" in result
        mock_execute.assert_called_once()


class TestRedashTools:
    """Redash LangChain Tools 테스트"""

    @patch("api.redash.list_dashboards")
    def test_list_redash_dashboards_tool(self, mock_list):
        """Redash 대시보드 목록 조회 tool 테스트"""
        mock_list.return_value = {
            "results": [
                {
                    "name": "Sales Dashboard",
                    "slug": "sales-dashboard",
                    "tags": ["sales", "metrics"],
                }
            ]
        }

        result = redash_tools.list_redash_dashboards.func()

        assert "Sales Dashboard" in result

    @patch("api.redash.list_dashboards")
    def test_list_redash_dashboards_empty(self, mock_list):
        """빈 대시보드 목록 테스트"""
        mock_list.return_value = {"results": []}

        result = redash_tools.list_redash_dashboards.func()

        assert "검색 결과가 없습니다" in result

    @patch("api.redash.get_dashboard")
    def test_read_redash_dashboard_tool(self, mock_get_dashboard):
        """Redash 대시보드 읽기 tool 테스트 - 쿼리 목록만 반환"""
        mock_get_dashboard.return_value = {
            "name": "Test Dashboard",
            "widgets": [
                {
                    "visualization": {
                        "query": {
                            "id": 123,
                            "name": "Test Query",
                        }
                    }
                }
            ],
        }

        result = redash_tools.read_redash_dashboard.func(dashboard_id=123)

        assert "Test Dashboard" in result
        assert "Query ID 123" in result
        assert "Test Query" in result

    @patch("api.redash.get_dashboard")
    def test_read_redash_dashboard_textbox(self, mock_get_dashboard):
        """Redash 대시보드 읽기 tool 테스트 - textbox 위젯 포함"""
        mock_get_dashboard.return_value = {
            "name": "Dashboard with Textbox",
            "widgets": [
                {"text": "# 안내사항\n이 대시보드는 매출 현황을 보여줍니다."},
                {
                    "visualization": {
                        "query": {
                            "id": 456,
                            "name": "매출 쿼리",
                        }
                    }
                },
            ],
        }

        result = redash_tools.read_redash_dashboard.func(dashboard_id=1)

        assert "Dashboard with Textbox" in result
        assert "안내사항" in result
        assert "매출 현황" in result
        assert "Query ID 456" in result
        assert "매출 쿼리" in result

    @patch("api.redash.get_query")
    def test_read_redash_query_tool(self, mock_get_query):
        """Redash 쿼리 상세 조회 tool 테스트"""
        mock_get_query.return_value = {
            "id": 123,
            "name": "Test Query",
            "query": "SELECT * FROM analytics.users",
            "data_source_id": 1,
        }

        result = redash_tools.read_redash_query.func(query_id=123)

        assert "Test Query" in result
        assert "analytics.users" in result


USERS_LIST_RESPONSE = {
    "members": [
        {"id": "U1", "real_name": "김분석"},
        {"id": "U2", "real_name": "이데이터"},
    ]
}


class TestCollectThreadContext:
    """스레드 대화 수집 테스트"""

    @patch("app.data_bot.slack_users_list", new_callable=AsyncMock)
    async def test_excludes_last_message(self, mock_users_list):
        """마지막 메시지(방금 도착한 질문)는 이전 대화에서 제외한다"""
        mock_users_list.return_value = USERS_LIST_RESPONSE
        client = MagicMock()
        client.conversations_replies = AsyncMock(
            return_value={
                "messages": [
                    {"user": "U1", "text": "지난주 DAU 알려줘"},
                    {"bot_id": "B1", "text": "1,000명입니다"},
                    {"user": "U1", "text": "이번주는?"},
                ]
            }
        )

        user_real_name, threads_joined = await data_bot.collect_thread_context(
            client, "D123", "1234567890.123456", "U1"
        )

        assert user_real_name == "김분석"
        assert "지난주 DAU 알려줘" in threads_joined
        assert "Bot:\n1,000명입니다" in threads_joined
        assert "이번주는?" not in threads_joined

    @patch("app.data_bot.slack_users_list", new_callable=AsyncMock)
    async def test_first_message_has_no_context(self, mock_users_list):
        """첫 질문이면 이전 대화가 비어 있다"""
        mock_users_list.return_value = USERS_LIST_RESPONSE
        client = MagicMock()
        client.conversations_replies = AsyncMock(
            return_value={"messages": [{"user": "U2", "text": "안녕"}]}
        )

        user_real_name, threads_joined = await data_bot.collect_thread_context(
            client, "D123", "1234567890.123456", "U2"
        )

        assert user_real_name == "이데이터"
        assert threads_joined == ""


async def authorize_stub(**kwargs):
    """토큰 검증 없이 인가된 것으로 처리한다"""
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-test",
        bot_id="B1",
        bot_user_id="U0BOT",
    )


def build_data_app():
    """실제 Bolt 미들웨어를 태운 데이터 봇 앱을 만든다"""
    app = AsyncApp(
        signing_secret="dummy",
        authorize=authorize_stub,
        request_verification_enabled=False,
    )
    data_bot.register_data_handlers(app)
    app.client.conversations_replies = AsyncMock(
        return_value={"messages": [{"user": "U1", "text": "어제 DAU 알려줘"}]}
    )
    return app


async def dispatch(app, event):
    """이벤트를 앱에 흘려보내고 리스너가 끝날 때까지 기다린다"""
    # 중복 이벤트 필터에 걸리지 않도록 이벤트마다 다른 event_id를 만든다
    body = {
        "token": "x",
        "team_id": "T1",
        "api_app_id": "A1",
        "type": "event_callback",
        "event_time": 1,
        "event_id": "Ev" + "".join(f"{k}{v}" for k, v in sorted(event.items())),
        "event": event,
    }
    before = set(asyncio.all_tasks())
    await app.async_dispatch(
        AsyncBoltRequest(body=json.dumps(body), mode="socket_mode")
    )
    # Bolt는 리스너를 태스크로 띄우고 즉시 응답하므로 새로 생긴 태스크를 기다린다
    spawned = [
        t for t in asyncio.all_tasks() - before if t is not asyncio.current_task()
    ]
    if spawned:
        await asyncio.gather(*spawned)


def dm_event(**overrides):
    """DM 메시지 이벤트를 만든다"""
    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D123",
        "user": "U1",
        "ts": "1234567890.123456",
        "text": "어제 DAU 알려줘",
    }
    event.update(overrides)
    return event


class TestDataBotDirectMessage:
    """DM 질문 처리 테스트 (실제 Bolt 미들웨어 경유)"""

    @patch("app.data_bot.answer_data_analysis", new_callable=AsyncMock)
    @patch("app.data_bot.slack_users_list", new_callable=AsyncMock)
    async def test_classic_dm(self, mock_users_list, mock_answer):
        """스레드 없는 DM은 질문 메시지의 스레드에서 처리한다"""
        mock_users_list.return_value = USERS_LIST_RESPONSE
        app = build_data_app()

        await dispatch(app, dm_event())

        assert mock_answer.call_args.args[:5] == (
            "1234567890.123456",
            "D123",
            "김분석",
            "",
            "어제 DAU 알려줘",
        )

    @patch("app.data_bot.answer_data_analysis", new_callable=AsyncMock)
    @patch("app.data_bot.slack_users_list", new_callable=AsyncMock)
    async def test_thread_reply(self, mock_users_list, mock_answer):
        """스레드에서 이어진 질문은 그 스레드에서 처리한다"""
        mock_users_list.return_value = USERS_LIST_RESPONSE
        app = build_data_app()

        await dispatch(
            app, dm_event(ts="1234567899.000000", thread_ts="1234567890.123456")
        )

        assert mock_answer.call_args.args[0] == "1234567890.123456"

    @patch("app.data_bot.answer_data_analysis", new_callable=AsyncMock)
    async def test_ignores_channel_message(self, mock_answer):
        """채널 메시지는 멘션 핸들러가 담당하므로 무시한다"""
        app = build_data_app()

        await dispatch(app, dm_event(channel_type="channel", channel="C123"))

        assert not mock_answer.called

    @patch("app.data_bot.answer_data_analysis", new_callable=AsyncMock)
    async def test_ignores_bot_and_edited_messages(self, mock_answer):
        """봇 메시지와 편집 이벤트는 무시한다 (무한 루프 방지)"""
        app = build_data_app()

        await dispatch(app, dm_event(bot_id="B9"))
        await dispatch(app, dm_event(subtype="message_changed"))

        assert not mock_answer.called


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
