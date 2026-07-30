"""브릿지 순수 로직 테스트 — 세션 고정, 재개 분기, 프롬프트 조립, 분할"""

import bridge


def test_session_id_is_stable_per_thread():
    """같은 스레드는 항상 같은 세션 ID, 다른 스레드는 다른 ID"""
    first = bridge.session_id_for("C1", "1700000001.0")
    assert first == bridge.session_id_for("C1", "1700000001.0")
    assert first != bridge.session_id_for("C1", "1700000002.0")
    assert first != bridge.session_id_for("C2", "1700000001.0")


def test_resume_switches_the_session_flag():
    """세션이 없으면 --session-id로 만들고, 있으면 --resume으로 잇는다"""
    new = bridge.claude_args("질문", "sid-1", resume=False)
    assert new[new.index("--session-id") + 1] == "sid-1"
    assert "--resume" not in new

    again = bridge.claude_args("질문", "sid-1", resume=True)
    assert again[again.index("--resume") + 1] == "sid-1"
    assert "--session-id" not in again


def test_prompt_carries_slack_location_without_mention():
    """멘션은 지우고 채널·스레드·요청자는 남긴다"""
    prompt = bridge.build_prompt("<@U0BOT> 연수 일정 알려줘", "C1", "1700000001.0", "U1")
    assert "<@U0BOT>" not in prompt
    assert "연수 일정 알려줘" in prompt
    assert "C1" in prompt and "1700000001.0" in prompt and "<@U1>" in prompt


def test_long_answer_is_split_and_empty_answer_survives():
    """슬랙 길이 제한으로 자르고, 빈 응답도 한 조각은 남긴다"""
    parts = bridge.chunks("가" * (bridge.MAX_SLACK_TEXT_CHARS + 10))
    assert len(parts) == 2
    assert "".join(parts) == "가" * (bridge.MAX_SLACK_TEXT_CHARS + 10)
    assert bridge.chunks("") == [""]


if __name__ == "__main__":
    test_session_id_is_stable_per_thread()
    test_resume_switches_the_session_flag()
    test_prompt_carries_slack_location_without_mention()
    test_long_answer_is_split_and_empty_answer_survives()
    print("ok")
