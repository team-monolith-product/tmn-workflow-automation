"""
데이터 분석 도구 묶음입니다.

팀모노리스 로봇(app/general.py), 데이터봇(app/data_bot.py), MCP(app/robot_mcp.py)가
모두 여기서 받습니다. 셋이 같은 도구를 쓰는 것을 조립하는 곳을 하나로 두어
보장합니다 -- 따로 조립하면 한쪽에만 도구가 생기거나 빠집니다.
"""

from collections.abc import Awaitable, Callable

from langchain_core.tools import BaseTool

from app.tools.athena_tools import get_execute_athena_query_tool
from app.tools.python_tools import get_execute_python_tool
from app.tools.redash_tools import (
    list_redash_dashboards,
    read_redash_dashboard,
    read_redash_query,
)
from app.tools.slack_place import SlackPlace


def get_data_tools(
    place: SlackPlace | None,
    *,
    draft_sms: Callable[..., Awaitable[str]] | None = None,
) -> list[BaseTool]:
    """Redash·Athena·파이썬 도구를 돌려줍니다.

    Args:
        place: 결과를 올릴 슬랙 스레드. 없으면 결과는 도구 반환값으로만 돌아갑니다
        draft_sms: 파이썬 코드 안에서 부를 문자 초안 코루틴

    Returns:
        list[BaseTool]: 데이터 분석 도구
    """
    return [
        list_redash_dashboards,
        read_redash_dashboard,
        read_redash_query,
        get_execute_athena_query_tool(place),
        get_execute_python_tool(place, draft_sms=draft_sms),
    ]
