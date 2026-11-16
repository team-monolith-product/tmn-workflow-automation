"""도구 결과를 올릴 슬랙 스레드 테스트"""

from unittest.mock import AsyncMock

from app.tools.slack_place import SlackPlace


async def test_post_는_그_스레드에_올리고_올린_글을_돌려준다():
    """ToolStatusHandler 는 say 가 돌려준 글의 ts 로 상태 카드를 고칩니다."""
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "1700000000.000200"}
    place = SlackPlace(client, "C123", "1700000000.000100")

    response = await place.post({"blocks": [{"type": "divider"}]})

    assert response["ts"] == "1700000000.000200"
    client.chat_postMessage.assert_awaited_once_with(
        channel="C123",
        thread_ts="1700000000.000100",
        text="로봇 답변",
        blocks=[{"type": "divider"}],
    )


async def test_post_는_say_처럼_다른_스레드를_받는다():
    client = AsyncMock()
    place = SlackPlace(client, "C123", "1700000000.000100")

    await place.post({"text": "안녕"}, thread_ts="1700000000.000300")

    client.chat_postMessage.assert_awaited_once_with(
        channel="C123", thread_ts="1700000000.000300", text="안녕"
    )
