"""
팀모노리스 로봇의 데이터 조회 도구를 MCP로 엽니다.

슬랙에서 로봇에게 시키는 데이터 조회를 개인 Claude·Codex 에서도 할 수 있게
합니다. 도구를 새로 짜지 않고 로봇이 쓰는 Redash 도구를 그대로 씁니다.

    - Redash: 대시보드 찾기·읽기, 쿼리 읽기. 로봇과 같은 도구다
    - Athena: 조회 문만 실행하는 읽기 전용 도구. 로봇의 Athena 는 결과를 슬랙
      스레드에 올리고 문장을 가리지 않으므로 MCP 에는 따로 만든 것을 쓴다

파이썬 실행은 열지 않습니다. 서버에서 제한 없이 돌아서, MCP 로 열면 서버의
비밀키가 드러납니다. 집계는 개인 Claude 의 로컬 파이썬으로 합니다.

LangChain 도구의 원래 함수를 그대로 등록합니다. 인자 설명은 Field 로 달아 두어
MCP SDK 도 로봇과 같은 입력 스키마를 만듭니다.
"""

from langchain_core.tools import StructuredTool
from mcp.server.mcpserver import MCPServer

from app.tools.athena_tools import read_only_athena_query
from app.tools.data_guide import DATA_GUIDE
from app.tools.redash_tools import (
    list_redash_dashboards,
    read_redash_dashboard,
    read_redash_query,
)

ROBOT_MCP_INSTRUCTIONS = (
    "팀모노리스 로봇(슬랙 대표 봇)의 데이터 조회 도구입니다.\n"
    "- Redash 대시보드·쿼리를 찾아 읽고, Athena 에 조회 쿼리를 실행합니다.\n"
    "- Athena 는 조회 문만 실행합니다. 쿼리는 Athena(Presto) SQL 문법으로 씁니다.\n"
    "- 결과는 슬랙으로 가지 않습니다. 직접 표로 정리해 보여줍니다." + DATA_GUIDE
)


def robot_tools() -> list[StructuredTool]:
    """MCP 에 여는 로봇 도구입니다.

    Returns:
        list[StructuredTool]: Redash 도구와 읽기 전용 Athena 도구
    """
    return [
        list_redash_dashboards,
        read_redash_dashboard,
        read_redash_query,
        read_only_athena_query,
    ]


def register_robot_tools(mcp: MCPServer) -> None:
    """로봇 도구를 MCP 서버에 등록합니다.

    Args:
        mcp: 등록할 서버
    """
    for lc_tool in robot_tools():
        mcp.add_tool(
            lc_tool.coroutine, name=lc_tool.name, description=lc_tool.description
        )
