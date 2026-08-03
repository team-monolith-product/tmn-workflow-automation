"""지식베이스 스레드 정규화 테스트"""

import json
from datetime import timezone

from service.knowledge.ingest import (
    build_raw_text,
    build_thread_row,
    compute_content_hash,
)

CHANNEL_ID = "C0AP8CG1Y6N"
DOMAIN = "monolith-keb2010.slack.com"

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
    assert _meta(messages)["participants"] == ["U02HT4EU4VD", "U02JLCWGETT"]


def test_distill_after는_마지막_활동_기준으로_미뤄진다():
    row = _row(delay=900)
    assert (row["distill_after"] - row["source_updated_at"]).total_seconds() == 900


def test_source_created_at은_부모_ts를_UTC로_변환한다():
    created = _row()["source_created_at"]
    assert created.tzinfo == timezone.utc
    assert created.timestamp() == 1785338608.347569


def test_raw는_원본_메시지를_그대로_보존한다():
    assert json.loads(_row()["raw"]) == THREAD


def test_raw_text는_작성자를_함께_남긴다():
    text = build_raw_text(THREAD)
    assert "U02HT4EU4VD: 세레브라스 놀리지 관리" in text
    assert "U02JLCWGETT: 확인해볼게요" in text


def test_content_hash는_답글이_붙으면_바뀐다():
    before = compute_content_hash(build_raw_text(THREAD[:1]))
    after = compute_content_hash(build_raw_text(THREAD))
    assert before != after


def test_content_hash는_같은_내용이면_동일하다():
    assert compute_content_hash(build_raw_text(THREAD)) == compute_content_hash(
        build_raw_text(THREAD)
    )
