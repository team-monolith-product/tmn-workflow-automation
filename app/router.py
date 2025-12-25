"""
질문 라우터 - 질문을 분석하여 적절한 Agent로 라우팅
"""

from typing import Literal
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage


async def route_question(question: str) -> Literal["data_analysis", "general"]:
    """
    질문을 분석하여 data_analysis 또는 general agent로 라우팅합니다.

    Args:
        question: 사용자 질문

    Returns:
        "data_analysis" 또는 "general"
    """
    router_llm = ChatOpenAI(model="gpt-4.1", temperature=0)

    system_prompt = """당신은 질문을 분류하는 라우터입니다.

질문을 분석하여 다음 두 카테고리 중 하나로 분류하세요:

1. **data_analysis**: 다음과 같은 질문들
   - 사내 지표, KPI, 비즈니스 메트릭 관련
   - 데이터 분석, SQL 쿼리, 데이터베이스 조회
   - 시계열 데이터, 통계, 트렌드 분석
   - 사용자 행동 분석, 매출 분석, 전환율 등
   - "~가 얼마야?", "~의 추이는?", "~를 분석해줘" 등

2. **general**: 다음과 같은 질문들
   - 노션 관련 작업 (페이지 생성, 검색, 업데이트)
   - 웹 검색, 일반 정보 조회
   - 일반 대화, 업무 협의
   - 기술 문서 조회

응답은 반드시 "data_analysis" 또는 "general" 중 하나만 출력하세요."""

    messages = [SystemMessage(content=system_prompt), HumanMessage(content=question)]

    response = await router_llm.ainvoke(messages)
    result = response.content.strip().lower()

    if "data_analysis" in result:
        return "data_analysis"
    else:
        return "general"
