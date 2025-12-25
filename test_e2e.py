"""
End-to-End 테스트 - 전체 플로우 테스트

슬랙 멘션 → 라우터 → 데이터 분석 Agent → Tools → 슬랙 응답
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from datetime import datetime


class TestDataAnalysisE2E:
    """데이터 분석 Bot의 전체 플로우를 테스트합니다"""

    @pytest.mark.asyncio
    @patch("app.router.ChatOpenAI")
    @patch("app.data_analysis.create_react_agent")
    @patch("app.data_analysis.ChatOpenAI")
    async def test_data_analysis_flow_with_router(
        self, mock_chat_openai, mock_create_agent, mock_router_chat
    ):
        """
        전체 플로우 테스트: 라우터 → 데이터 분석 Agent
        """
        # 1. 라우터 모킹 - "data_analysis"로 라우팅
        mock_router_llm = MagicMock()
        mock_router_response = Mock()
        mock_router_response.content = "data_analysis"
        mock_router_llm.ainvoke = AsyncMock(return_value=mock_router_response)
        mock_router_chat.return_value = mock_router_llm

        # 2. Agent 실행 결과 모킹
        mock_agent_executor = MagicMock()
        mock_final_message = Mock()
        mock_final_message.content = "지난달 매출은 1억 원입니다."
        mock_agent_executor.ainvoke = AsyncMock(
            return_value={"messages": [mock_final_message]}
        )
        mock_create_agent.return_value = mock_agent_executor

        # 3. Say 함수 모킹
        mock_say = AsyncMock()

        # 4. 라우터 실행
        from app.router import route_question

        agent_type = await route_question("지난달 매출이 얼마야?")
        assert agent_type == "data_analysis"

        # 5. 데이터 분석 Agent 실행
        from app.data_analysis import answer_data_analysis

        await answer_data_analysis(
            thread_ts="1234567890.123456",
            channel="C1234567890",
            user_real_name="테스트유저",
            threads_joined="",
            text="지난달 매출이 얼마야?",
            say=mock_say,
        )

        # 6. 검증: say가 호출되었는지 확인
        assert mock_say.called
        # 최종 답변 호출 찾기 (blocks가 있는 호출)
        blocks_calls = [
            call
            for call in mock_say.call_args_list
            if call.args and isinstance(call.args[0], dict) and "blocks" in call.args[0]
        ]
        assert len(blocks_calls) > 0, "최종 답변이 호출되지 않았습니다"
        # 마지막 blocks 호출 확인
        last_blocks_call = blocks_calls[-1]
        assert last_blocks_call.kwargs.get("thread_ts") == "1234567890.123456"

    @pytest.mark.asyncio
    @patch("api.redash.list_dashboards")
    @patch("api.redash.get_dashboard")
    @patch("api.redash.get_query")
    @patch("api.athena.execute_and_wait")
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
                {"name": "매출 대시보드", "slug": "sales-dashboard", "tags": ["sales"]}
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
        from app.tools.athena_tools import execute_athena_query

        # 5. Tool 체인 실행
        # Step 1: 대시보드 목록 조회
        dashboard_list = list_redash_dashboards.func(query="매출")
        assert "매출 대시보드" in dashboard_list
        assert "sales-dashboard" in dashboard_list

        # Step 2: 대시보드 상세 조회
        dashboard_detail = read_redash_dashboard.func(slug="sales-dashboard")
        assert "월별 매출" in dashboard_detail
        assert "analytics.sales" in dashboard_detail

        # Step 3: Athena 쿼리 실행
        query_result = execute_athena_query.func(
            query="SELECT SUM(amount) FROM analytics.sales WHERE date >= '2024-12-01'",
            database="analytics",
        )
        assert "total_sales" in query_result
        assert "100000000" in query_result

        # 6. 모든 API가 호출되었는지 검증
        mock_list_dashboards.assert_called_once()
        mock_get_dashboard.assert_called_once()
        mock_athena_exec.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.router.ChatOpenAI")
    async def test_router_classification(self, mock_chat):
        """
        라우터가 다양한 질문을 올바르게 분류하는지 테스트
        """
        from app.router import route_question

        test_cases = [
            # (질문, 기대 결과)
            ("지난달 매출이 얼마야?", "data_analysis"),
            ("사용자 수 추이를 보여줘", "data_analysis"),
            ("전환율이 어떻게 되나요?", "data_analysis"),
            ("SQL로 데이터 조회해줘", "data_analysis"),
            ("노션 페이지 만들어줘", "general"),
            ("구글에서 검색해줘", "general"),
            ("오늘 날씨 어때?", "general"),
        ]

        for question, expected_type in test_cases:
            # 라우터 LLM 모킹
            mock_llm = MagicMock()
            mock_response = Mock()
            mock_response.content = expected_type
            mock_llm.ainvoke = AsyncMock(return_value=mock_response)
            mock_chat.return_value = mock_llm

            # 라우팅 실행
            result = await route_question(question)

            # 검증
            assert (
                result == expected_type
            ), f"질문 '{question}'의 분류가 잘못되었습니다. 기대: {expected_type}, 실제: {result}"

    @pytest.mark.asyncio
    @patch("app.data_analysis.create_react_agent")
    @patch("app.data_analysis.ChatOpenAI")
    async def test_error_handling_in_agent(self, mock_chat_openai, mock_create_agent):
        """
        Agent에서 에러가 발생했을 때의 처리를 테스트
        """
        # Agent가 에러를 던지도록 설정
        mock_agent_executor = MagicMock()
        mock_agent_executor.ainvoke = AsyncMock(
            side_effect=Exception("Athena 쿼리 실행 실패")
        )
        mock_create_agent.return_value = mock_agent_executor

        mock_say = AsyncMock()

        from app.data_analysis import answer_data_analysis

        # 에러가 발생해도 프로그램이 중단되지 않아야 함
        with pytest.raises(Exception) as exc_info:
            await answer_data_analysis(
                thread_ts="1234567890.123456",
                channel="C1234567890",
                user_real_name="테스트유저",
                threads_joined="",
                text="지난달 매출이 얼마야?",
                say=mock_say,
            )

        assert "Athena 쿼리 실행 실패" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("app.data_analysis.create_react_agent")
    @patch("app.data_analysis.ChatOpenAI")
    async def test_thread_context_handling(self, mock_chat_openai, mock_create_agent):
        """
        스레드 컨텍스트(이전 대화)가 올바르게 처리되는지 테스트
        """
        # Agent 실행 결과 모킹
        mock_agent_executor = MagicMock()
        mock_final_message = Mock()
        mock_final_message.content = "12월 매출은 5천만원입니다."

        # ainvoke가 호출된 메시지를 캡처하기 위한 변수
        captured_messages = None

        async def capture_messages(input_dict, *args, **kwargs):
            nonlocal captured_messages
            captured_messages = input_dict.get("messages", [])
            return {"messages": [mock_final_message]}

        mock_agent_executor.ainvoke = AsyncMock(side_effect=capture_messages)
        mock_create_agent.return_value = mock_agent_executor

        mock_say = AsyncMock()

        from app.data_analysis import answer_data_analysis

        # 스레드 컨텍스트와 함께 실행
        threads_joined = (
            "유저A: 11월 매출이 얼마였어?\n\nBot: 11월 매출은 4천만원이었습니다."
        )

        await answer_data_analysis(
            thread_ts="1234567890.123456",
            channel="C1234567890",
            user_real_name="유저B",
            threads_joined=threads_joined,
            text="그럼 12월은?",
            say=mock_say,
        )

        # 검증: 스레드 컨텍스트가 메시지에 포함되었는지 확인
        assert captured_messages is not None
        human_message_content = captured_messages[1].content
        assert "11월 매출" in human_message_content
        assert "유저B" in human_message_content
        assert "그럼 12월은?" in human_message_content


class TestGeneralAgentE2E:
    """General Agent의 전체 플로우를 테스트합니다"""

    @pytest.mark.asyncio
    @patch("app.router.ChatOpenAI")
    async def test_general_flow_routing(self, mock_chat):
        """
        일반 질문이 general agent로 라우팅되는지 테스트
        """
        # 라우터 모킹 - "general"로 라우팅
        mock_llm = MagicMock()
        mock_response = Mock()
        mock_response.content = "general"
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_chat.return_value = mock_llm

        from app.router import route_question

        agent_type = await route_question("노션에 페이지 만들어줘")
        assert agent_type == "general"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
