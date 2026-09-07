"""
범용 봇 이벤트 핸들러 테스트
"""

import asyncio
import json

import pytest
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request.async_request import AsyncBoltRequest
from unittest.mock import AsyncMock, patch

from app import general


async def authorize_stub(**kwargs):
    """토큰 검증 없이 인가된 것으로 처리한다"""
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-test",
        bot_id="B1",
        bot_user_id="U0BOT",
    )


def build_general_app():
    """실제 Bolt 미들웨어를 태운 범용 봇 앱을 만든다"""
    app = AsyncApp(
        signing_secret="dummy",
        authorize=authorize_stub,
        request_verification_enabled=False,
    )
    general.register_general_handlers(app)
    return app


async def dispatch(app, event):
    """이벤트를 앱에 흘려보내고 리스너가 끝날 때까지 기다린다"""
    # 중복 이벤트 필터에 걸리지 않도록 이벤트마다 다른 event_id를 만든다
    body = {
        "token": "x",
        "team_id": "T1",
        "api_app_id": "A1",
        "type": "event_callback",
        "event_time": 1,
        "event_id": "Ev" + "".join(f"{k}{v}" for k, v in sorted(event.items())),
        "event": event,
    }
    before = set(asyncio.all_tasks())
    await app.async_dispatch(
        AsyncBoltRequest(body=json.dumps(body), mode="socket_mode")
    )
    # Bolt는 리스너를 태스크로 띄우고 즉시 응답하므로 새로 생긴 태스크를 기다린다
    spawned = [
        t for t in asyncio.all_tasks() - before if t is not asyncio.current_task()
    ]
    if spawned:
        await asyncio.gather(*spawned)


def dm_event(**overrides):
    """DM 메시지 이벤트를 만든다"""
    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D123",
        "user": "U1",
        "ts": "1234567890.123456",
        "text": "이번 주 할 일 정리해줘",
    }
    event.update(overrides)
    return event


@pytest.fixture
def mock_answer():
    with patch("app.general.answer", new_callable=AsyncMock) as answer, patch(
        "app.general._build_tools", new_callable=AsyncMock
    ) as build_tools:
        build_tools.return_value = []
        yield answer


class TestRunWaJob:
    """`/wa` 작업 결과 회신 테스트"""

    async def test_returns_string_to_caller(self):
        """작업이 문자열을 반환하면 실행한 사람에게 되돌려준다"""
        respond = AsyncMock()

        await general._run_wa_job(
            lambda: "이미 main 과 같습니다.", [], {}, "설명", respond
        )

        respond.assert_awaited_once_with("이미 main 과 같습니다.")

    async def test_keeps_non_string_result_silent(self):
        """문자열이 아닌 반환값은 회신 대상이 아니다"""
        respond = AsyncMock()

        await general._run_wa_job(lambda: True, [], {}, "설명", respond)

        assert not respond.called

    async def test_reports_failure_to_caller(self):
        """작업이 실패하면 파드 로그가 아니라 실행한 사람에게 알린다"""
        respond = AsyncMock()

        def fail():
            raise RuntimeError("404 Not Found")

        await general._run_wa_job(fail, [], {}, "develop 을 main 으로 초기화", respond)

        text = respond.await_args.args[0]
        assert "develop 을 main 으로 초기화 에 실패했습니다." in text
        assert "404 Not Found" in text


class TestGeneralBotDirectMessage:
    """DM 질문 처리 테스트 (실제 Bolt 미들웨어 경유)"""

    async def test_classic_dm(self, mock_answer):
        """스레드 없는 DM은 질문 메시지의 스레드에서 처리한다"""
        app = build_general_app()

        await dispatch(app, dm_event())

        assert mock_answer.call_args.args[:4] == (
            "1234567890.123456",
            "D123",
            "U1",
            "이번 주 할 일 정리해줘",
        )

    async def test_thread_reply(self, mock_answer):
        """스레드에서 이어진 질문은 그 스레드에서 처리한다"""
        app = build_general_app()

        await dispatch(
            app, dm_event(ts="1234567899.000000", thread_ts="1234567890.123456")
        )

        assert mock_answer.call_args.args[0] == "1234567890.123456"

    async def test_ignores_bot_and_edited_messages(self, mock_answer):
        """봇 메시지와 편집 이벤트는 무시한다 (무한 루프 방지)"""
        app = build_general_app()

        await dispatch(app, dm_event(bot_id="B9"))
        await dispatch(app, dm_event(subtype="message_changed"))

        assert not mock_answer.called

    async def test_ignores_channel_message(self, mock_answer):
        """채널 메시지는 DM 경로를 타지 않는다"""
        app = build_general_app()

        with patch("app.general.route_bug.route_bug", new_callable=AsyncMock):
            await dispatch(
                app,
                dm_event(
                    channel_type="channel",
                    channel=general.SLACK_BUG_REPORT_CHANNEL_ID,
                    subtype="bot_message",
                ),
            )

        assert not mock_answer.called
