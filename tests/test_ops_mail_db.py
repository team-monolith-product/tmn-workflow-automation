"""ops@ 메일 store 의 SQL 을 실제 Postgres 에 돌려보는 테스트

OPS_MAIL_TEST_DATABASE_URL 이 없으면 건너뜁니다. 테스트마다 스키마를 새로 만들어
007 마이그레이션을 적용하고, 끝나면 지웁니다.
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from service.db import connect, fetch_all
from service.ops_mail import store

CHANNEL = "C_OPS"


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("OPS_MAIL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("OPS_MAIL_TEST_DATABASE_URL이 필요합니다")
    schema = "ops_mail_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        monkeypatch.setenv(
            "KNOWLEDGE_DATABASE_URL",
            make_conninfo(dsn, options=f"-c search_path={schema}"),
        )
        try:
            with connect() as conn:
                conn.execute(
                    (
                        Path(__file__).resolve().parents[1]
                        / "migrations/knowledge/007_ops_mail.sql"
                    ).read_text()
                )
            yield
        finally:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def _age_claim(ses_message_id: str, interval: str) -> None:
    """맡은 시각을 과거로 돌립니다."""
    with connect() as conn:
        conn.execute(
            "UPDATE ops_mail_inbox SET claimed_at = now() - %s::interval"
            " WHERE ses_message_id = %s",
            (interval, ses_message_id),
        )


def _row(ses_message_id: str, **overrides) -> store.OpsMailRow:
    values = {
        "direction": "in",
        "message_id": f"<{ses_message_id}@abc.kr>",
        "ses_message_id": ses_message_id,
        "counterpart": "office@abc.kr",
        "from_address": "office@abc.kr",
        "to_addresses": ["ops@team-mono.com"],
        "subject": "견적 문의",
        "body_text": "본문",
        "references_ids": [],
        "channel_id": CHANNEL,
        "sent_by": None,
    }
    return store.OpsMailRow(**{**values, **overrides})


def test_원본은_자리를_잡은_동안_다시_맡지_못하고_횟수를_다_쓰면_멈춘다(database):
    # When : 처음 맡고, 바로 다시 맡으려 하고, 자리를 잡은 지 오래된 뒤 다시 맡으면
    first = store.claim("m1", "ops/m1")
    during_lease = store.claim("m1", "ops/m1")
    attempts = [first]
    for _ in range(3):
        _age_claim("m1", "1 hour")
        attempts.append(store.claim("m1", "ops/m1"))

    # Then : 자리를 잡은 동안은 못 맡고, 세 번까지만 맡는다
    assert during_lease is None
    assert attempts == [1, 2, 3, None]
    assert store.settled(["m1", "other"]) == {"m1"}


def test_끝낸_원본은_다시_맡지_않고_메일_기록과_끝_표시가_함께_남는다(database):
    store.claim("m1", "ops/m1")

    # When : 처리를 끝내고 나서 시간이 지나 다시 맡으려 하면
    store.finish(_row("m1"))
    _age_claim("m1", "1 hour")

    # Then : 다시 맡지 않는다
    assert store.claim("m1", "ops/m1") is None
    with connect() as conn:
        assert len(fetch_all(conn, "SELECT * FROM ops_mail_message")) == 1


def test_끝내기가_실패하면_메일_기록도_남지_않는다(database, monkeypatch):
    store.claim("m1", "ops/m1")
    # 메일 기록은 들어간 뒤, 끝 표시가 실패하게 한다
    monkeypatch.setattr(
        store, "FINISH", "UPDATE ops_mail_inbox SET nope = 1 WHERE ses_message_id = %s"
    )

    # When : 끝내기의 두 번째 단계가 실패하면
    with pytest.raises(psycopg.errors.UndefinedColumn):
        store.finish(_row("m1"))

    # Then : 먼저 넣은 메일 기록도 함께 되돌아가, 다시 맡을 때 중복으로 남지 않는다
    assert store.settled(["m1"]) == set()
    with connect() as conn:
        assert fetch_all(conn, "SELECT * FROM ops_mail_message") == []


def test_포기한_원본은_한_번만_알리고_하루가_지난_원본도_포기로_본다(database):
    for ses_id in ("tried", "running", "old"):
        store.claim(ses_id, f"ops/{ses_id}")
    # 세 번 맡아 끝내지 못한 원본
    for _ in range(2):
        _age_claim("tried", "1 hour")
        store.claim("tried", "ops/tried")
    store.fail("tried", "ValueError: 깨진 MIME")
    _age_claim("tried", "1 hour")
    # 한 번만 맡았지만 하루가 지난 원본(그동안 정리 작업이 돌지 못함)
    _age_claim("old", "2 days")

    # When : 포기한 원본을 찾고, 알렸다고 표시한 뒤 다시 찾으면
    given_up = store.given_up()
    for row in given_up:
        store.mark_alerted(row["ses_message_id"])

    # Then : 처리 중인 원본은 빼고, 이유와 함께 한 번만 나온다
    assert {
        (r["ses_message_id"], r["attempts"], r["last_error"]) for r in given_up
    } == {("tried", 3, "ValueError: 깨진 MIME"), ("old", 1, None)}
    assert store.given_up() == []
    # 알린 원본은 정리 작업이 거르고, 누가 다시 불러도 맡지 못한다. "old" 는
    # 횟수가 남았지만 포기를 알렸으니 다시 올리면 알림과 메일이 엇갈린다
    assert store.settled(["tried", "old", "running"]) == {"tried", "old"}
    assert store.claim("old", "ops/old") is None


def test_답장은_그_상대에게서_마지막으로_받은_사람이_쓴_메일에_건다(database):
    store.claim_thread(CHANNEL, "office@abc.kr", "ts-office")
    store.insert(_row("in-1", references_ids=["<m0@abc.kr>"]))
    # 그 뒤에 채널에서 여럿에게 보낸 새 메일과, 상대의 부재중 자동응답
    store.insert(
        _row(
            "out-1",
            direction="out",
            message_id="<ours@team-mono.com>",
            subject="11월 소식",
            sent_by="U1",
        ),
    )
    store.insert(_row("auto-1", subject="부재중", auto_reply=True))

    # When : 상대방 스레드의 답장 대상을 찾으면
    target = store.reply_target(CHANNEL, "ts-office")

    # Then : 보낸 메일과 자동응답은 건너뛰고 사람이 보낸 마지막 메일에 건다
    assert target["counterpart"] == "office@abc.kr"
    assert target["subject"] == "견적 문의"
    assert target["message_id"] == "<in-1@abc.kr>"
    assert target["references_ids"] == ["<m0@abc.kr>"]


def test_나중에_다시_처리된_옛_메일보다_실제로_최근에_온_메일에_건다(database):
    store.claim_thread(CHANNEL, "office@abc.kr", "ts-office")
    store.insert(
        _row(
            "new", subject="Re: 일정", received_at=datetime(2026, 11, 4, 9, tzinfo=UTC)
        ),
    )
    # 먼저 왔지만 처리에 실패했다가 정리 작업이 나중에 다시 넣은 메일
    store.insert(
        _row(
            "old", subject="견적 문의", received_at=datetime(2026, 11, 3, 9, tzinfo=UTC)
        ),
    )

    # When : 상대방 스레드의 답장 대상을 찾으면
    target = store.reply_target(CHANNEL, "ts-office")

    # Then : 넣은 순서가 아니라 우리가 받은 순서로 가장 최근 메일에 건다
    assert target["subject"] == "Re: 일정"


def test_상대방_스레드인데_받은_메일_기록이_없으면_상대만_돌려준다(database):
    store.claim_thread(CHANNEL, "office@abc.kr", "ts-office")

    # When : 스레드만 있고 받은 메일 기록이 없는 스레드와, 메일 스레드가 아닌 곳을 찾으면
    target = store.reply_target(CHANNEL, "ts-office")
    other = store.reply_target(CHANNEL, "ts-chat")

    # Then : 상대는 알려주되 답장할 메일은 없고, 메일 스레드가 아니면 None 이다
    assert target["counterpart"] == "office@abc.kr"
    assert target["message_id"] is None
    assert other is None


def test_스레드를_먼저_연_쪽이_이긴다(database):
    # When : 같은 상대의 스레드를 두 쪽이 차례로 등록하면
    first = store.claim_thread(CHANNEL, "office@abc.kr", "ts-1")
    second = store.claim_thread(CHANNEL, "office@abc.kr", "ts-2")

    # Then : 둘 다 먼저 연 스레드를 받는다
    assert first == second == "ts-1"
