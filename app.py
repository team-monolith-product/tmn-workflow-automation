"""
슬랙에서 로봇을 멘션하여 답변을 얻고, 노션에 작업을 생성하거나 업데이트하는 기능을 제공하는 슬랙 봇입니다.
"""

import asyncio
import os

import sentry_sdk
from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp, AsyncAssistant
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

from app.general import register_general_handlers
from app.contents import register_contents_handlers
from app.data_bot import register_data_handlers
from app.justin import register_justin_handlers

# 환경 변수 로드
load_dotenv()

sentry_sdk.init(dsn=os.environ.get("SENTRY_DSN", ""))

# 앱 초기화
app = AsyncApp(token=os.environ.get("SLACK_BOT_TOKEN"))
app_contents = AsyncApp(token=os.environ.get("SLACK_BOT_TOKEN_CONTENTS"))
app_data = AsyncApp(token=os.environ.get("SLACK_BOT_TOKEN_DATA"))
app_justin = AsyncApp(token=os.environ.get("SLACK_BOT_TOKEN_JUSTIN"))
assistant = AsyncAssistant()

# 이벤트 핸들러 등록
register_general_handlers(app, assistant)
register_contents_handlers(app_contents)
register_data_handlers(app_data)
register_justin_handlers(app_justin)


async def main():
    # Assistant 등록
    app.use(assistant)

    # Async Socket Mode Handler
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    bot_coroutine = handler.start_async()

    contents_coroutine = AsyncSocketModeHandler(
        app_contents, os.environ["SLACK_APP_TOKEN_CONTENTS"]
    ).start_async()

    data_coroutine = AsyncSocketModeHandler(
        app_data, os.environ["SLACK_APP_TOKEN_DATA"]
    ).start_async()

    justin_coroutine = AsyncSocketModeHandler(
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
