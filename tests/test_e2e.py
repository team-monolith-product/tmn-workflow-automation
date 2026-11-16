import pytest
from unittest.mock import patch, AsyncMock


class TestDataAnalysisTools:
    @pytest.mark.asyncio
    @patch("api.redash.list_dashboards", new_callable=AsyncMock)
    @patch("api.redash.get_dashboard", new_callable=AsyncMock)
    @patch("api.redash.get_query", new_callable=AsyncMock)
    @patch("api.athena.execute_and_wait", new_callable=AsyncMock)
    async def test_full_tool_chain(
        self, mock_athena_exec, mock_get_query, mock_get_dashboard, mock_list_dashboards
    ):
        """
        전체 Tool 체인 테스트:
        1. list_redash_dashboards 호출
        2. read_redash_dashboard 호출
        3. execute_athena_query 호출
        """
        # 1. Redash 대시보드 목록 모킹
        mock_list_dashboards.return_value = {
            "results": [
                {
                    "id": 1,
                    "name": "매출 대시보드",
                    "slug": "sales-dashboard",
                    "tags": ["sales"],
                }
            ]
        }

        # 2. Redash 대시보드 상세 모킹
        mock_get_dashboard.return_value = {
            "name": "매출 대시보드",
            "widgets": [
                {
                    "visualization": {
                        "query": {
                            "id": 123,
                            "name": "월별 매출",
                            "query": "SELECT SUM(amount) FROM analytics.sales WHERE date >= '2024-12-01'",
                        }
                    }
                }
            ],
        }

        mock_get_query.return_value = {
            "data_source_id": 1,
            "options": {"data_source": "Analytics DB"},
        }

        # 3. Athena 실행 결과 모킹
        mock_athena_exec.return_value = {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "total_sales"}]},
                    {"Data": [{"VarCharValue": "100000000"}]},
                ]
            }
        }

        # 4. Tools 임포트 및 실행
        from app.tools.redash_tools import list_redash_dashboards, read_redash_dashboard
        from app.tools.athena_tools import get_execute_athena_query_tool

        # 5. Tool 체인 실행
        # Step 1: 대시보드 목록 조회
        dashboard_list = await list_redash_dashboards.ainvoke({})
        assert "매출 대시보드" in dashboard_list
        assert "ID 1" in dashboard_list

        # Step 2: 대시보드 상세 조회 (dashboard_id 사용)
        dashboard_detail = await read_redash_dashboard.ainvoke({"dashboard_id": 1})
        assert "월별 매출" in dashboard_detail

        # Step 3: Athena 쿼리 실행 (async tool)
        execute_athena_query = get_execute_athena_query_tool()
        query_result = await execute_athena_query.ainvoke(
            {
                "query": "SELECT SUM(amount) FROM analytics.sales WHERE date >= '2024-12-01'",
                "database": "analytics",
            }
        )
        assert "total_sales" in query_result
        assert "100000000" in query_result

        # 6. 모든 API가 호출되었는지 검증
        mock_list_dashboards.assert_called_once()
        mock_athena_exec.assert_called_once()
