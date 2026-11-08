"""
팀모노리스 로봇의 도구를 MCP로 엽니다.

슬랙에서 로봇에게 시킬 수 있는 일은 Claude·Codex 같은 CLI 에이전트도 할 수
있어야 합니다. 도구를 새로 짜지 않고 로봇이 쓰는 도구를 그대로 씁니다. 로봇에
도구가 늘면 여기도 따라 늘어야 하므로, 목록을 따로 들고 있지 않고 로봇의
build_robot_tools 를 그대로 부릅니다.

도구는 두 갈래로 엽니다.

    ① 어디서 물어도 똑같이 도는 도구 (Athena·Redash·파이썬·노션 페이지)
       → MCP 도구로 바로 등록합니다. 로봇이 쓰는 get_data_tools·get_notion_page_tools
         를 스레드 없이(place=None) 그대로 부릅니다. 웹 검색과 페이지 읽기는
         Claude·Codex 에 이미 있어 열지 않습니다.
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

from cachetools import TTLCache
from langchain_core.tools import BaseTool
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.general import DATA_GUIDE, build_robot_tools, thread_say
from app.mcp_common import AdminToken
from app.tools.data_tools import get_data_tools
from app.tools.notion_page_tools import get_notion_page_tools
from service.slack import get_email_to_user_id_async

ROBOT_MCP_INSTRUCTIONS = (
    "팀모노리스 로봇(슬랙 대표 봇)이 쓰는 도구입니다. 슬랙에서 로봇에게 시킬 수 있는 일을 "
    "여기서 할 수 있습니다.\n"
    "- Athena·Redash·파이썬·노션 페이지 도구는 바로 부릅니다.\n"
    "- 문자·메일 초안, 작업 예약, 채널 공지, 노션 작업 만들기, 채널 작업 리스트, "
    "지식 수집 켜고 끄기는 슬랙 스레드에 묶여 있습니다. 사용자에게 스레드 링크를 받아 "
    "list_robot_tools 로 그 스레드에서 쓸 수 있는 도구와 입력 스키마를 확인한 뒤 "
    "call_robot_tool 로 부릅니다. 스레드 링크를 지어내지 않습니다.\n"
    "- 스레드 없이 부른 도구의 결과는 슬랙으로 가지 않습니다. Athena 결과는 직접 표로 "
    "정리해 보여주고, 파이썬에서는 차트 대신 집계 값을 print 합니다." + DATA_GUIDE
)

# 시간 상한. 코드 실행 워커는 봇 넷이 나눠 쓰는 하나뿐이다.
MCP_PYTHON_TIMEOUT_SECONDS = 300

# [링크 복사] 한 주소. 엔터프라이즈 도메인은 점이 더 들어간다.
# https://<workspace>.slack.com/archives/C123/p1700000000123456[?thread_ts=...&cid=...]
_PERMALINK_RE = re.compile(
    r"^https://[\w.-]+\.slack\.com/archives/(?P<channel>[CGD][A-Z0-9]+)"
    r"/p(?P<sec>\d{10})(?P<frac>\d{6})(?:\?(?P<query>\S*))?$"
)
# 브라우저에서 스레드를 연 주소창 주소.
# https://app.slack.com/client/T123/C123/thread/C123-1700000000.123456
_CLIENT_THREAD_RE = re.compile(
    r"^https://app\.slack\.com/client/[A-Z0-9]+/(?P<channel>[CGD][A-Z0-9]+)"
    r"/thread/(?P=channel)-(?P<ts>\d{10}\.\d{6})(?:[/?]\S*)?$"
)
_THREAD_TS_RE = re.compile(r"(?:^|&)thread_ts=(?P<ts>\d{10}\.\d{6})(?:&|$)")


def parse_slack_thread_url(url: str) -> tuple[str, str]:
    """슬랙 메시지 링크에서 채널과 스레드 타임스탬프를 꺼냅니다.

    [링크 복사] 한 주소와 브라우저 주소창의 스레드 주소를 받습니다. 답글 링크면
    그 답글이 아니라 부모 스레드를 가리킵니다.

    Args:
        url: 슬랙 메시지 링크

    Returns:
        tuple[str, str]: (채널 ID, 스레드 ts)
    """
    client = _CLIENT_THREAD_RE.match(url.strip())
    if client:
        return client["channel"], client["ts"]
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


def _register_langchain_tool(mcp: MCPServer, lc_tool: BaseTool) -> None:
    """LangChain 도구를 같은 이름·설명·입력 스키마의 MCP 도구로 등록합니다.

    MCP SDK는 함수 시그니처로 스키마를 만듭니다. 도구의 args_schema 필드로
    시그니처를 지어 붙여, 로봇이 보는 것과 같은 스키마가 나가게 합니다.

    Args:
        mcp: 등록할 서버
        lc_tool: 로봇 도구
    """
    schema = cast(Any, lc_tool.args_schema)
    params = []
    for name, field in schema.model_fields.items():
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
    """도구 반환값을 MCP 응답 글자로 바꿉니다.

    Args:
        result: 도구 반환값. 노션 상태 변경처럼 None 을 돌려주는 도구가 있습니다

    Returns:
        str: MCP 응답 글자
    """
    if result is None:
        return "완료했습니다."
    if isinstance(result, str):
        return result
    # 목록·사전을 돌려주는 도구가 있다. str() 은 파이썬 표기라 읽기 어렵다.
    return json.dumps(result, ensure_ascii=False, default=str)


def placeless_tools() -> list[BaseTool]:
    """슬랙 스레드 없이 똑같이 도는 로봇 도구입니다.

    로봇의 build_robot_tools 가 쓰는 것과 같은 함수를 스레드 없이 부릅니다. 웹
    검색과 페이지 읽기는 MCP 클라이언트에 이미 있어 뺍니다.

    Returns:
        list[BaseTool]: 로봇 도구 목록
    """
    return get_notion_page_tools() + get_data_tools(
        None, python_timeout_seconds=MCP_PYTHON_TIMEOUT_SECONDS
    )


def _requester_email() -> str:
    """MCP 토큰의 이메일을 꺼냅니다.

    Returns:
        str: admin-rails 계정 이메일
    """
    return cast(AdminToken, get_access_token()).email


async def _requester(slack: AsyncWebClient) -> str:
    """MCP 토큰의 이메일로 슬랙 사용자 ID를 찾습니다.

    전체 사용자 목록은 하루 캐시라 그 뒤에 들어온 사람은 없습니다. 목록에 없으면
    슬랙에 이메일로 한 번 더 묻습니다.

    Args:
        slack: 대표 봇 슬랙 클라이언트

    Returns:
        str: 슬랙 사용자 ID
    """
    email = _requester_email()
    # admin-rails 와 슬랙 프로필의 대소문자가 다를 수 있다. 메일 주소는 대소문자를 가리지 않는다.
    users = {
        address.lower(): user_id
        for address, user_id in (await get_email_to_user_id_async(slack)).items()
    }
    if email.lower() in users:
        return users[email.lower()]
    try:
        return (await slack.users_lookupByEmail(email=email))["user"]["id"]
    except SlackApiError as error:
        if error.response["error"] != "users_not_found":
            raise
        raise ToolError(f"{email} 로 가입한 슬랙 사용자가 없습니다.") from error


async def _is_member(slack: AsyncWebClient, channel: str, user_id: str) -> bool:
    """사용자가 채널에 들어가 있는지 봅니다.

    Args:
        slack: 대표 봇 슬랙 클라이언트
        channel: 채널 ID
        user_id: 슬랙 사용자 ID

    Returns:
        bool: 들어가 있으면 True. 로봇이 볼 수 없는 채널도 False 입니다
    """
    cursor = None
    while True:
        try:
            response = await slack.conversations_members(
                channel=channel, limit=1000, cursor=cursor
            )
        except SlackApiError as error:
            # 로봇이 없는 비공개 채널과 남의 DM 은 로봇에게 없는 채널이다.
            if error.response["error"] != "channel_not_found":
                raise
            return False
        if user_id in response["members"]:
            return True
        cursor = response["response_metadata"]["next_cursor"]
        if not cursor:
            return False


async def _check_place(slack: AsyncWebClient, channel: str, user_id: str) -> None:
    """질문자가 그 채널에 있고 로봇이 그 채널에 글을 올릴 수 있는지 봅니다.

    질문자 쪽을 먼저 봅니다. 채널에 없는 사람에게는 채널이 없든, 로봇이 없든
    같은 문구를 돌려줍니다 -- 문구가 다르면 비공개 채널이 있는지, 로봇이
    들어가 있는지가 바깥에 드러납니다.

    Args:
        slack: 대표 봇 슬랙 클라이언트
        channel: 채널 ID
        user_id: 질문자의 슬랙 사용자 ID
    """
    if not await _is_member(slack, channel, user_id):
        raise ToolError(
            "들어가 있지 않은 채널이거나 로봇이 볼 수 없는 채널입니다. "
            "비공개 채널이면 로봇을 초대했는지 확인해 주십시오."
        )
    info = (await slack.conversations_info(channel=channel))["channel"]
    # DM 과 그룹 DM 은 로봇이 보인다는 것 자체가 참여자라는 뜻이다. is_member 가 없다.
    if info["is_im"] or info["is_mpim"]:
        return
    # 공개 채널은 로봇이 없어도 조회되지만 글을 올릴 때 not_in_channel 로 막힌다.
    if not info["is_member"]:
        raise ToolError(
            "로봇이 들어가 있지 않은 채널입니다. 로봇을 채널에 초대해 주십시오."
        )


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
    await _check_place(slack, channel, user_id)

    say = thread_say(slack, channel, thread_ts)
    return await build_robot_tools(slack, user_id, channel, thread_ts, say)


def register_robot_tools(mcp: MCPServer, slack: AsyncWebClient) -> None:
    """로봇 도구를 MCP 서버에 등록합니다.

    Args:
        mcp: 등록할 서버
        slack: 대표 봇 슬랙 클라이언트
    """
    for lc_tool in placeless_tools():
        _register_langchain_tool(mcp, lc_tool)

    # 스레드 도구는 만들 때마다 슬랙·DB·노션을 여러 번 부른다. list_robot_tools 로
    # 만든 것을 바로 이은 call_robot_tool 이 한 번 꺼내 쓴다. 꺼내면 지우므로 다음
    # 호출은 새로 만든다 -- 작업 리스트를 켜는 것처럼 도구 구성이 바뀌는 호출 뒤에
    # 옛 구성이 남지 않는다.
    listed: TTLCache[tuple[str, str, str], list[BaseTool]] = TTLCache(
        maxsize=100, ttl=300
    )

    def listed_key(slack_thread_url: str) -> tuple[str, str, str]:
        """질문자와 스레드로 묶습니다. 답글 링크와 부모 링크는 같은 키입니다."""
        return (_requester_email(), *parse_slack_thread_url(slack_thread_url))

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
        listed[listed_key(slack_thread_url)] = tools
        return "\n\n".join(
            f"## {t.name}\n{t.description}\n입력: "
            + json.dumps(t.tool_call_schema.model_json_schema(), ensure_ascii=False)
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
        built = listed.pop(listed_key(slack_thread_url), None)
        if built is None:
            built = await thread_tools(slack, slack_thread_url)
        tools = {t.name: t for t in built}
        if name not in tools:
            raise ToolError(
                f"이 스레드에서는 {name} 도구를 쓸 수 없습니다. "
                f"쓸 수 있는 도구: {', '.join(tools)}"
            )
        return _as_text(await tools[name].ainvoke(arguments))
