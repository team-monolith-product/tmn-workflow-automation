"""
팀모노리스 로봇의 도구를 MCP로 엽니다.

슬랙에서 로봇에게 시킬 수 있는 일은 Claude·Codex 같은 CLI 에이전트도 할 수
있어야 합니다. 도구를 새로 짜지 않고 로봇이 쓰는 도구를 그대로 씁니다. 로봇에
도구가 늘면 여기도 따라 늘어야 하므로, 목록을 따로 들고 있지 않고 로봇의
_build_tools 를 그대로 부릅니다.

도구는 두 갈래로 엽니다.

    ① 어디서 물어도 똑같이 도는 도구 (Athena·Redash·파이썬·노션 페이지·웹)
       → MCP 도구로 바로 등록합니다. 슬랙 스레드 없이 부릅니다.
    ② 슬랙 스레드에 묶인 도구 (문자·메일 초안, 예약, 채널 공지, 노션 작업,
       채널 작업 리스트, 지식 수집 켜고 끄기)
       → 스레드 링크를 받아 list_robot_tools·call_robot_tool 로 부릅니다.
         로봇은 멘션이 온 채널과 질문한 사람으로 도구를 만들고, 채널과 스쿼드에
         따라 도구 구성과 입력 스키마가 달라집니다. 그래서 고정 목록으로 등록하지
         않고 부를 때마다 그 스레드 기준으로 다시 만듭니다.

②에서 질문한 사람은 MCP 토큰의 이메일로 찾은 슬랙 사용자입니다. 슬랙에서 그
사람이 그 스레드에 멘션한 것과 같은 도구가 만들어지고, 문자·메일은 여전히
스레드의 카드에서 사람이 [보내기] 를 눌러야 나갑니다. 사람이 들어가 있지 않은
채널의 스레드는 받지 않습니다 -- 로봇 이름으로 남의 채널에 글을 올리는 길이 된다.
"""

import inspect
import json
import re
from typing import Annotated, Any, cast

from langchain_core.tools import BaseTool
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.common import get_notion_page_tool, get_web_page_from_url, search_tool
from app.general import ROBOT_INSTRUCTIONS, _build_tools, thread_say
from app.mcp_common import AdminToken
from app.tools.athena_tools import get_execute_athena_query_tool
from app.tools.notion_page_tools import get_notion_page_copy_tools
from app.tools.python_tools import get_execute_python_tool
from app.tools.redash_tools import (
    list_redash_dashboards,
    read_redash_dashboard,
    read_redash_query,
)
from service.slack import get_email_to_user_id_async

# 슬랙 결과 전송 스위치. 스레드가 없으면 아무 일도 하지 않으므로 스키마에서 뺀다.
HIDDEN_PLACELESS_ARGS = frozenset({"show_result_to_user"})

ROBOT_MCP_INSTRUCTIONS = (
    "팀모노리스 로봇(슬랙 대표 봇)이 쓰는 도구입니다. 슬랙에서 로봇에게 시킬 수 있는 일을 "
    "여기서 할 수 있습니다.\n"
    "- Athena·Redash·파이썬·노션 페이지 도구는 바로 부릅니다.\n"
    "- 문자·메일 초안, 작업 예약, 채널 공지, 노션 작업 만들기, 채널 작업 리스트, "
    "지식 수집 켜고 끄기는 슬랙 스레드에 묶여 있습니다. 사용자에게 스레드 링크를 받아 "
    "list_robot_tools 로 그 스레드에서 쓸 수 있는 도구와 입력 스키마를 확인한 뒤 "
    "call_robot_tool 로 부릅니다. 스레드 링크를 지어내지 않습니다." + ROBOT_INSTRUCTIONS
)

# https://<workspace>.slack.com/archives/C123/p1700000000123456[?thread_ts=...&cid=...]
_PERMALINK_RE = re.compile(
    r"^https://[\w-]+\.slack\.com/archives/(?P<channel>[CGD][A-Z0-9]+)"
    r"/p(?P<sec>\d{10})(?P<frac>\d{6})(?:\?(?P<query>\S*))?$"
)
_THREAD_TS_RE = re.compile(r"(?:^|&)thread_ts=(?P<ts>\d{10}\.\d{6})(?:&|$)")


def parse_slack_thread_url(url: str) -> tuple[str, str]:
    """슬랙 메시지 링크에서 채널과 스레드 타임스탬프를 꺼냅니다.

    답글 링크면 그 답글이 아니라 부모 스레드를 가리킵니다.

    Args:
        url: 슬랙 메시지 링크

    Returns:
        tuple[str, str]: (채널 ID, 스레드 ts)
    """
    match = _PERMALINK_RE.match(url.strip())
    if match is None:
        raise ToolError(
            "슬랙 메시지 링크가 아닙니다. 스레드의 메시지에서 [링크 복사] 한 주소를 주십시오: "
            f"{url}"
        )
    thread_ts = f"{match['sec']}.{match['frac']}"
    if match["query"]:
        parent = _THREAD_TS_RE.search(match["query"])
        if parent:
            thread_ts = parent["ts"]
    return match["channel"], thread_ts


def _register_langchain_tool(
    mcp: MCPServer, lc_tool: BaseTool, hidden: frozenset[str] = frozenset()
) -> None:
    """LangChain 도구를 같은 이름·설명·입력 스키마의 MCP 도구로 등록합니다.

    MCP SDK는 함수 시그니처로 스키마를 만듭니다. 도구의 args_schema 필드로
    시그니처를 지어 붙여, 로봇이 보는 것과 같은 스키마가 나가게 합니다.

    Args:
        mcp: 등록할 서버
        lc_tool: 로봇 도구
        hidden: 스키마에서 뺄 인자. 기본값으로 실행됩니다.
    """
    schema = cast(Any, lc_tool.args_schema)
    params = []
    for name, field in schema.model_fields.items():
        if name in hidden:
            continue
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Annotated[field.annotation, field],
                default=(
                    inspect.Parameter.empty if field.is_required() else field.default
                ),
            )
        )

    async def run(**kwargs: Any) -> str:
        return _as_text(await lc_tool.ainvoke(kwargs))

    run.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
    run.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
    run.__name__ = lc_tool.name
    mcp.add_tool(run, name=lc_tool.name, description=lc_tool.description)


def _as_text(result: Any) -> str:
    """도구 반환값을 MCP 응답 글자로 바꿉니다. 노션 상태 변경처럼 None 을 돌려주는 도구가 있습니다."""
    if result is None:
        return "완료했습니다."
    if isinstance(result, str):
        return result
    # 웹 검색처럼 목록을 돌려주는 도구가 있다. str() 은 파이썬 표기라 읽기 어렵다.
    return json.dumps(result, ensure_ascii=False, default=str)


def placeless_tools() -> list[BaseTool]:
    """슬랙 스레드 없이 똑같이 도는 로봇 도구입니다.

    _build_tools 의 도구 중 채널·스레드·질문자를 받지 않는 것들입니다.
    """
    return [
        search_tool,
        get_web_page_from_url,
        get_notion_page_tool(),
        *get_notion_page_copy_tools(),
        get_execute_python_tool(),
        list_redash_dashboards,
        read_redash_dashboard,
        read_redash_query,
        get_execute_athena_query_tool(),
    ]


async def _requester(slack: AsyncWebClient) -> str:
    """MCP 토큰의 이메일로 슬랙 사용자 ID를 찾습니다."""
    email = cast(AdminToken, get_access_token()).email
    # admin-rails 와 슬랙 프로필의 대소문자가 다를 수 있다. 메일 주소는 대소문자를 가리지 않는다.
    users = {
        address.lower(): user_id
        for address, user_id in (await get_email_to_user_id_async(slack)).items()
    }
    user_id = users.get(email.lower())
    if user_id is None:
        raise ToolError(f"{email} 로 가입한 슬랙 사용자가 없습니다.")
    return user_id


async def _check_channel(slack: AsyncWebClient, channel: str) -> None:
    """로봇이 그 채널에 글을 올릴 수 있는지 봅니다.

    로봇이 없는 비공개 채널과 남의 DM 은 channel_not_found 로 옵니다. 공개 채널은
    로봇이 없어도 조회되지만 글을 올릴 때 not_in_channel 로 막힙니다.
    """
    try:
        info = (await slack.conversations_info(channel=channel))["channel"]
    except SlackApiError as error:
        if error.response["error"] != "channel_not_found":
            raise
        raise ToolError(
            "로봇이 볼 수 없는 채널입니다. 로봇을 채널에 초대해 주십시오."
        ) from error
    if not info.get("is_im") and not info["is_member"]:
        raise ToolError(
            "로봇이 들어가 있지 않은 채널입니다. 로봇을 채널에 초대해 주십시오."
        )


async def _is_member(slack: AsyncWebClient, channel: str, user_id: str) -> bool:
    """사용자가 채널에 들어가 있는지 봅니다."""
    cursor = None
    while True:
        response = await slack.conversations_members(
            channel=channel, limit=1000, cursor=cursor
        )
        if user_id in response["members"]:
            return True
        cursor = response["response_metadata"]["next_cursor"]
        if not cursor:
            return False


async def thread_tools(slack: AsyncWebClient, slack_thread_url: str) -> list[BaseTool]:
    """그 스레드에서 질문자가 로봇을 불렀을 때와 같은 도구를 만듭니다.

    Args:
        slack: 대표 봇 슬랙 클라이언트
        slack_thread_url: 슬랙 스레드의 메시지 링크

    Returns:
        list[BaseTool]: 로봇 도구 목록
    """
    channel, thread_ts = parse_slack_thread_url(slack_thread_url)
    user_id = await _requester(slack)
    await _check_channel(slack, channel)
    if not await _is_member(slack, channel, user_id):
        raise ToolError("들어가 있지 않은 채널의 스레드입니다.")

    say = thread_say(slack, channel, thread_ts)
    return await _build_tools(slack, user_id, channel, thread_ts, say)


def register_robot_tools(mcp: MCPServer, slack: AsyncWebClient) -> None:
    """로봇 도구를 MCP 서버에 등록합니다.

    Args:
        mcp: 등록할 서버
        slack: 대표 봇 슬랙 클라이언트
    """
    for lc_tool in placeless_tools():
        _register_langchain_tool(mcp, lc_tool, HIDDEN_PLACELESS_ARGS)

    @mcp.tool(
        description=(
            "슬랙 스레드에서 팀모노리스 로봇이 쓸 수 있는 도구와 각 도구의 입력 "
            "스키마(JSON Schema)를 돌려줍니다. 문자·메일 초안, 작업 예약·조회·취소, "
            "채널 공지, 노션 작업 만들기, 채널 작업 리스트, 지식 수집 켜고 끄기처럼 "
            "스레드에 묶인 일을 하기 전에 부릅니다. 도구 구성은 채널과 질문자의 "
            "스쿼드에 따라 다릅니다. slack_thread_url 은 사용자에게 받은 슬랙 "
            "메시지 링크입니다."
        )
    )
    async def list_robot_tools(slack_thread_url: str) -> str:
        tools = await thread_tools(slack, slack_thread_url)
        return "\n\n".join(
            f"## {t.name}\n{t.description}\n입력: {t.tool_call_schema.model_json_schema()}"
            for t in tools
        )

    @mcp.tool(
        description=(
            "슬랙 스레드에서 팀모노리스 로봇의 도구를 하나 실행합니다. name 과 "
            "arguments 는 list_robot_tools 가 돌려준 이름·스키마를 따릅니다. 결과물은 "
            "그 스레드에 올라갑니다. 문자·메일은 스레드에 초안 카드만 올리고, 사람이 "
            "카드에서 [보내기] 를 눌러야 나갑니다."
        )
    )
    async def call_robot_tool(
        slack_thread_url: str, name: str, arguments: dict[str, Any]
    ) -> str:
        tools = {t.name: t for t in await thread_tools(slack, slack_thread_url)}
        if name not in tools:
            raise ToolError(
                f"이 스레드에서는 {name} 도구를 쓸 수 없습니다. "
                f"쓸 수 있는 도구: {', '.join(tools)}"
            )
        return _as_text(await tools[name].ainvoke(arguments))
