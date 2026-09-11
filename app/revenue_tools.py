"""
매출 facts 를 MCP 도구로 내놓는다.

지식베이스 MCP(/mcp)에 얹는다. 따로 서버를 세우지 않는 이유는 직원마다 MCP
설정을 하나 더 붙이게 하지 않으려는 것이다 — 이미 붙어 있는 서버에 도구가
늘어나는 쪽이 아무 설정 없이 바로 쓰인다.

누가 무엇을 읽었는지는 admin-rails 토큰의 이메일로 남긴다. 매출 데이터에는
고객 실명·좌석당 단가·계약기간이 들어 있다. 사내용이다.
"""

import asyncio
from typing import Literal, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer

from app.mcp_common import AdminToken
from service.revenue.facts import get_facts
from service.revenue.render import (
    MAX_ROWS,
    render_health,
    render_summary,
    render_transactions,
)

INSTRUCTIONS = """
매출 도구 셋(revenue_summary · revenue_transactions · revenue_health)은 팀모노리스
매출장 구글시트를 그대로 읽어 만든 숫자다. 반드시 지킬 것:
1. 금액은 전부 공급가액이다. 부가세 제외. 합계(공급가+세액)와 섞지 않는다.
2. 매출은 세금계산서 발행 실적만이다. 「예정」은 계약은 됐으나 계산서가 안 나간
   금액이라 총계에 없다. 더하려면 「확정매출(발행+예정)」이라고 이름을 붙인다.
3. 연도는 매출장 탭 기준 귀속연도다. 발행일 연도와 다른 이연 건이 있다.
4. 대분류·세부분류의 정본은 매출장 「분류마스터」 탭이다. 세부분류는 2026년분에만
   있다 — 그 이전은 결측이 아니라 원래 없는 것이니 세부분류로 연도 비교를 하지 않는다.
5. revenue_health 에 경고가 있으면 그 숫자를 확정으로 쓰지 않는다.
6. 고객 실명·좌석당 단가·계약기간이 들어 있다. 사내용이다. 외부 문서에 옮기지 않는다.
""".strip()

SUMMARY_DESCRIPTION = (
    "팀모노리스 매출 요약. 연도별 총계와 한 해의 상품 분류·세부분류·월별 집계, "
    "파이프라인(예정) 총액을 표로 돌려준다. 금액은 공급가액(부가세 제외), 세금계산서 "
    "발행 실적만이다. year 를 생략하면 가장 최근 연도. 먼저 이걸 읽고 거래 단위가 "
    "필요할 때 revenue_transactions 를 부른다."
)

TRANSACTIONS_DESCRIPTION = (
    "매출 거래 목록. 조건에 맞는 거래를 금액 큰 순으로 표로 돌려준다(발행일·거래처·"
    "품목·수량·단가·공급가액·대분류·세부분류·비고). year 는 귀속연도, category 는 "
    "대분류·세부분류·어느 뷰의 버킷이든 부분일치, customer 는 거래처명 부분일치. "
    "status 는 issued(발행분, 기본)·pipeline(예정분)·all. "
    f"limit 기본 100, 최대 {MAX_ROWS}."
)

HEALTH_DESCRIPTION = (
    "매출 숫자의 검증 경고 목록. 연도 총계가 기준값과 맞는지, 규칙에 안 걸린 미분류가 "
    "있는지, 매출장 행이 분류마스터를 벗어났는지를 본다. 비어 있으면 숫자를 믿어도 "
    "된다. 경고가 있으면 해소 전까지 그 숫자를 확정으로 쓰지 않는다."
)


def _actor() -> str:
    return cast(AdminToken, get_access_token()).email


def register_revenue_tools(mcp: MCPServer) -> None:
    """지식베이스 MCP 서버에 매출 도구 셋을 등록한다."""

    @mcp.tool(description=SUMMARY_DESCRIPTION)
    async def revenue_summary(year: int | None = None) -> str:
        actor = _actor()
        facts = await asyncio.to_thread(get_facts)
        print(f"[revenue] summary year={year} by {actor}")
        return render_summary(facts, year)

    @mcp.tool(description=TRANSACTIONS_DESCRIPTION)
    async def revenue_transactions(
        year: int | None = None,
        category: str | None = None,
        customer: str | None = None,
        status: Literal["issued", "pipeline", "all"] = "issued",
        limit: int = 100,
    ) -> str:
        actor = _actor()
        facts = await asyncio.to_thread(get_facts)
        print(
            f"[revenue] transactions year={year} category={category!r}"
            f" customer={customer!r} status={status} by {actor}"
        )
        return render_transactions(facts, year, category, customer, status, limit)

    @mcp.tool(description=HEALTH_DESCRIPTION)
    async def revenue_health() -> str:
        actor = _actor()
        facts = await asyncio.to_thread(get_facts)
        print(f"[revenue] health by {actor}")
        return render_health(facts)
