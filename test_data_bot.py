"""
데이터 분석 Bot 관련 모듈들의 단위 테스트
"""

import pytest
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from api import athena, redash
from app.tools import athena_tools, redash_tools
from app import router


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
        formatted = athena_tools.format_query_results(results)
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
        formatted = athena_tools.format_query_results(results)

        assert "id" in formatted
        assert "name" in formatted
        assert "Alice" in formatted
        assert "Bob" in formatted
        assert "|" in formatted  # 마크다운 테이블 형식

    @patch("api.athena.execute_and_wait")
    def test_execute_athena_query_tool(self, mock_execute):
        """Athena 쿼리 실행 tool 테스트"""
        mock_execute.return_value = {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "count"}]},
                    {"Data": [{"VarCharValue": "42"}]},
                ]
            }
        }

        result = athena_tools.execute_athena_query.func(
            query="SELECT COUNT(*) as count FROM test", database="test_db"
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
        assert "sales-dashboard" in result
        assert "sales" in result

    @patch("api.redash.list_dashboards")
    def test_list_redash_dashboards_empty(self, mock_list):
        """빈 대시보드 목록 테스트"""
        mock_list.return_value = {"results": []}

        result = redash_tools.list_redash_dashboards.func()

        assert "검색 결과가 없습니다" in result

    @patch("api.redash.get_dashboard")
    @patch("api.redash.get_query")
    def test_read_redash_dashboard_tool(self, mock_get_query, mock_get_dashboard):
        """Redash 대시보드 읽기 tool 테스트"""
        mock_get_dashboard.return_value = {
            "name": "Test Dashboard",
            "widgets": [
                {
                    "visualization": {
                        "query": {
                            "id": 123,
                            "name": "Test Query",
                            "query": "SELECT * FROM analytics.users",
                        }
                    }
                }
            ],
        }
        mock_get_query.return_value = {
            "data_source_id": 1,
            "options": {"data_source": "Analytics DB"},
        }

        result = redash_tools.read_redash_dashboard.func(slug="test-dashboard")

        assert "Test Dashboard" in result
        assert "Test Query" in result
        assert "analytics.users" in result


class TestRouter:
    """질문 라우터 테스트"""

    @pytest.mark.asyncio
    @patch("app.router.ChatOpenAI")
    async def test_route_data_analysis_question(self, mock_chat):
        """데이터 분석 질문 라우팅 테스트"""
        mock_llm = MagicMock()
        mock_response = Mock()
        mock_response.content = "data_analysis"
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_chat.return_value = mock_llm

        result = await router.route_question("지난달 매출이 얼마야?")

        assert result == "data_analysis"

    @pytest.mark.asyncio
    @patch("app.router.ChatOpenAI")
    async def test_route_general_question(self, mock_chat):
        """일반 질문 라우팅 테스트"""
        mock_llm = MagicMock()
        mock_response = Mock()
        mock_response.content = "general"
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_chat.return_value = mock_llm

        result = await router.route_question("노션 페이지 만들어줘")

        assert result == "general"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
