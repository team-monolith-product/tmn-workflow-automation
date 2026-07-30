"""
Operate Agent 슬랙 브릿지

슬랙 멘션을 받아 컨테이너 안에서 claude를 실행하고 결과를 스레드에 올린다.
스레드마다 세션 ID가 고정되므로 같은 스레드의 후속 질문은 대화가 이어진다.
"""

import asyncio
import json
import os
import re
import uuid
from pathlib import Path

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

WORKSPACE = Path("/data/workspace")
SESSION_MARKERS = Path("/data/sessions")

# 스레드 → 세션 ID를 결정론적으로 만들기 위한 고정 네임스페이스
SESSION_NAMESPACE = uuid.UUID("1b9f2c3d-4e5f-46a7-8c9d-0e1f2a3b4c5d")

MODEL = os.environ.get("OPERATE_MODEL", "opus")
TIMEOUT_SECONDS = int(os.environ.get("OPERATE_TIMEOUT_SECONDS", "900"))
MAX_SLACK_TEXT_CHARS = 3000

_MENTION_RE = re.compile(r"<@[^>]+>")


def session_id_for(channel: str, thread_ts: str) -> str:
    """스레드마다 같은 세션 ID를 돌려준다. 매핑을 따로 저장할 필요가 없다."""
    return str(uuid.uuid5(SESSION_NAMESPACE, f"{channel}:{thread_ts}"))


def claude_args(prompt: str, session_id: str, resume: bool) -> list[str]:
    """
    claude 실행 인자를 만든다.

    Args:
        prompt: 사용자 요청
        session_id: 스레드에 대응하는 세션 ID
        resume: 이 세션이 이미 있으면 True

    Returns:
        list[str]: subprocess 인자
    """
    return [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        MODEL,
        "--permission-mode",
        "bypassPermissions",
        "--resume" if resume else "--session-id",
        session_id,
    ]


def build_prompt(text: str, channel: str, thread_ts: str, user: str) -> str:
    """멘션 텍스트에 슬랙 위치 정보를 붙인다. 에이전트가 이 채널을 직접 조회할 수 있어야 한다."""
    request = _MENTION_RE.sub("", text).strip()
    return (
        f"[슬랙] 채널 {channel} · 스레드 {thread_ts} · 요청자 <@{user}>\n\n{request}"
    )


def chunks(text: str) -> list[str]:
    """슬랙 메시지 길이 제한에 맞춰 자른다."""
    return [
        text[i : i + MAX_SLACK_TEXT_CHARS]
        for i in range(0, len(text), MAX_SLACK_TEXT_CHARS)
    ] or [""]


async def run_claude(prompt: str, session_id: str) -> str:
    """
    claude를 한 번 실행하고 최종 응답 텍스트를 돌려준다.

    Args:
        prompt: 슬랙 위치 정보가 붙은 요청
        session_id: 스레드에 대응하는 세션 ID

    Returns:
        str: 에이전트 응답 또는 실패 안내
    """
    marker = SESSION_MARKERS / session_id
    process = await asyncio.create_subprocess_exec(
        *claude_args(prompt, session_id, marker.exists()),
        cwd=WORKSPACE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return f"{TIMEOUT_SECONDS}초 안에 끝나지 않아 중단했습니다. 요청을 나눠서 다시 시도해 주세요."

    if process.returncode != 0:
        return (
            f"실행에 실패했습니다 (exit {process.returncode})\n"
            f"```\n{stderr.decode()[-1500:]}\n```"
        )

    marker.touch()
    return json.loads(stdout)["result"]


def main() -> None:
    """슬랙 소켓 모드로 멘션을 받는다."""
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN_OPERATE"])

    @app.event("app_mention")
    async def handle_mention(event, say, client):
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]

        await client.reactions_add(channel=channel, name="eyes", timestamp=event["ts"])

        answer = await run_claude(
            build_prompt(event["text"], channel, thread_ts, event["user"]),
            session_id_for(channel, thread_ts),
        )

        for chunk in chunks(answer):
            await say(text=chunk, thread_ts=thread_ts)

    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN_OPERATE"])
    asyncio.run(handler.start_async())


if __name__ == "__main__":
    main()
