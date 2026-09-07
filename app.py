"""
슬랙에서 로봇을 멘션하여 답변을 얻고, 노션에 작업을 생성하거나 업데이트하는 기능을 제공하는 슬랙 봇입니다.
"""

import asyncio
import os

import sentry_sdk
from aiohttp import ClientConnectionResetError
from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from app.socket_mode_handler import AsyncImmediateAckSocketModeHandler

from app.general import get_sms_revise, register_general_handlers
from app.knowledge import register_knowledge_middleware
from app.contents import register_contents_handlers
from app.data_bot import register_data_handlers
from app.justin import register_justin_handlers
from app.sms import register_sms_handlers
from service.db import get_dsn
from scheduler import start_scheduler

# 환경 변수 로드
load_dotenv()


def _before_send(event, hint):
    if "exc_info" in hint:
        _, exc_value, _ = hint["exc_info"]
        if isinstance(exc_value, ClientConnectionResetError):
            return None
    # 로그 이벤트 필터 (Slack Bolt가 내부적으로 catch 후 로깅하는 경우)
    message = (event.get("logentry") or {}).get("message", "")
    if not message:
        message = event.get("message", "")
    if "ClientConnectionResetError" in message:
        return None
    # Slack Socket Mode 세션 타임아웃/재연결 실패 로그 필터
    # (세션 ID가 매번 달라 이슈가 각각 생성되어 노이즈가 큼)
    if "Failed to check the current session" in message:
        return None
    return event


sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN", ""),
    before_send=_before_send,
)

# 앱 초기화
app = AsyncApp(token=os.environ.get("SLACK_BOT_TOKEN"))
app_contents = AsyncApp(token=os.environ.get("SLACK_BOT_TOKEN_CONTENTS"))
app_data = AsyncApp(token=os.environ.get("SLACK_BOT_TOKEN_DATA"))
app_justin = AsyncApp(token=os.environ.get("SLACK_BOT_TOKEN_JUSTIN"))

# 이벤트 핸들러 등록
register_general_handlers(app)
# 리스너가 아니라 미들웨어다. 대표 봇에 이미 message 리스너가 있어서
# 리스너를 추가하면 Bolt 디스패치가 둘 중 하나에서 멈춘다.
register_knowledge_middleware(app)
# 발송 승인 버튼. 초안은 도구가 올리고, 실제 발송은 이 핸들러가 한다.
# [수정] 피드백은 초안을 쓴 에이전트에게 되돌린다 — 콜백을 여기서 주입하는 것은
# 에이전트 진입점을 아는 곳이 여기뿐이고, sms 가 general 을 부르면 순환하기 때문이다.
register_sms_handlers(app, revise=get_sms_revise(app))
register_contents_handlers(app_contents)
register_data_handlers(app_data)
register_justin_handlers(app_justin)


async def main():
    # 채널 멘션 응답 경로가 이 접속을 읽는다. 미설정이면 첫 멘션에서
    # 전원 무응답으로 나타나므로 기동할 때 죽는 편이 낫다.
    get_dsn()

    # 스케줄러 시작 (이벤트 루프에 크론 작업 등록)
    start_scheduler()

    # Async Socket Mode Handler (즉시 ack로 이벤트 재전송 방지)
    handler = AsyncImmediateAckSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    bot_coroutine = handler.start_async()

    contents_coroutine = AsyncImmediateAckSocketModeHandler(
        app_contents, os.environ["SLACK_APP_TOKEN_CONTENTS"]
    ).start_async()

    data_coroutine = AsyncImmediateAckSocketModeHandler(
        app_data, os.environ["SLACK_APP_TOKEN_DATA"]
    ).start_async()

    justin_coroutine = AsyncImmediateAckSocketModeHandler(
        app_justin, os.environ["SLACK_APP_TOKEN_JUSTIN"]
    ).start_async()

    await asyncio.gather(
        bot_coroutine,
        contents_coroutine,
        data_coroutine,
        justin_coroutine,
    )


if __name__ == "__main__":
    asyncio.run(main())
