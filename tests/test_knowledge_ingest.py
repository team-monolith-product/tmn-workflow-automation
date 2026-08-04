"""지식베이스 스레드 정규화 테스트"""

import json
from datetime import timezone

from service.knowledge.ingest import (
    author_of,
    build_raw_text,
    build_thread_row,
    compute_content_hash,
    strip_redundant,
)

CHANNEL_ID = "C0AP8CG1Y6N"
DOMAIN = "monolith-keb2010.slack.com"
EMAILS = {"U02HT4EU4VD": "lch@team-mono.com", "U02JLCWGETT": "byb@team-mono.com"}

THREAD = [
    {
        "ts": "1785338608.347569",
        "user": "U02HT4EU4VD",
        "text": "세레브라스 놀리지 관리\n두 번째 줄",
        "reactions": [{"name": "eyes", "count": 2}],
    },
    {
        "ts": "1785338611.914149",
        "user": "U02JLCWGETT",
        "text": "확인해볼게요",
    },
]


def _row(messages=None, delay=900):
    return build_thread_row(
        data_source_id=7,
        channel_id=CHANNEL_ID,
        messages=messages or THREAD,
        workspace_domain=DOMAIN,
        distill_delay_seconds=delay,
        user_emails=EMAILS,
    )


def _meta(messages=None):
    """metadata jsonb를 파싱해 돌려줍니다."""
    return json.loads(_row(messages)["metadata"])


def test_external_id_는_채널과_부모_ts로_만들어진다():
    assert _row()["external_id"] == f"{CHANNEL_ID}:1785338608.347569"


def test_permalink은_ts의_점을_제거한다():
    assert _row()["url"] == (
        f"https://{DOMAIN}/archives/{CHANNEL_ID}/p1785338608347569"
    )


def test_title은_부모_메시지_첫_줄만_쓴다():
    assert _row()["title"] == "세레브라스 놀리지 관리"


def test_소스별_필드는_컬럼이_아니라_metadata에_있다():
    row = _row()
    assert "reply_count" not in row
    assert "reaction_count" not in row
    assert "participants" not in row
    assert set(json.loads(row["metadata"])) == {
        "channel_id",
        "participants",
        "reply_count",
        "reaction_count",
    }


def test_reply_count는_부모를_제외한다():
    assert _meta()["reply_count"] == 1


def test_reaction_count는_스레드_전체를_합산한다():
    assert _meta()["reaction_count"] == 2


def test_participants는_중복없이_정렬된다():
    messages = THREAD + [
        {"ts": "1785338620.000000", "user": "U02HT4EU4VD", "text": "네"}
    ]
    assert _meta(messages)["participants"] == ["byb@team-mono.com", "lch@team-mono.com"]


def test_distill_after는_마지막_활동_기준으로_미뤄진다():
    row = _row(delay=900)
    assert (row["distill_after"] - row["source_updated_at"]).total_seconds() == 900


def test_source_created_at은_부모_ts를_UTC로_변환한다():
    created = _row()["source_created_at"]
    assert created.tzinfo == timezone.utc
    assert created.timestamp() == 1785338608.347569


def test_raw는_원본_메시지를_그대로_보존한다():
    assert json.loads(_row()["raw"]) == THREAD


def test_raw_text는_작성자를_이메일로_남긴다():
    text = build_raw_text(THREAD, EMAILS)
    assert "lch@team-mono.com: 세레브라스 놀리지 관리" in text
    assert "byb@team-mono.com: 확인해볼게요" in text


def test_content_hash는_답글이_붙으면_바뀐다():
    before = compute_content_hash(build_raw_text(THREAD[:1], EMAILS))
    after = compute_content_hash(build_raw_text(THREAD, EMAILS))
    assert before != after


def test_content_hash는_같은_내용이면_동일하다():
    assert compute_content_hash(build_raw_text(THREAD, EMAILS)) == compute_content_hash(
        build_raw_text(THREAD, EMAILS)
    )


def test_이메일이_없는_봇은_이름으로_표기된다():
    bot = {
        "ts": "1.0",
        "bot_id": "B0907ST6HNV",
        "username": "AWS Health",
        "text": "알림",
    }
    assert author_of(bot, EMAILS) == "bot:AWS Health"


def test_매핑에_없는_사용자는_bot으로_떨어진다():
    assert author_of({"ts": "1.0", "user": "UNKNOWN1"}, EMAILS) == "bot:UNKNOWN1"


def test_사람이_참여한_스레드는_정제_대상이다():
    assert _row()["distill_state"] == "pending"


def test_봇_단독_스레드는_정제하지_않는다():
    messages = [
        {"ts": "1785338608.347569", "bot_id": "B0907ST6HNV", "text": "알림"},
        {"ts": "1785338609.000000", "bot_id": "B0907ST6HNV", "text": "재알림"},
    ]
    assert _row(messages)["distill_state"] == "skipped"


def test_봇_알림에_사람이_답글을_달면_정제_대상이_된다():
    messages = [
        {"ts": "1785338608.347569", "bot_id": "B0907ST6HNV", "text": "알림"},
        {"ts": "1785338609.000000", "user": "U02HT4EU4VD", "text": "원인은 이거"},
    ]
    assert _row(messages)["distill_state"] == "pending"


def test_raw는_프로필_사본과_blocks를_뺀다():
    m = {
        "ts": "1.0",
        "text": "안녕",
        "blocks": [{"type": "rich_text"}],
        "user_profile": {"real_name": "이창환"},
        "bot_profile": {"name": "봇"},
    }
    kept = strip_redundant(m)
    assert set(kept) == {"ts", "text"}
