"""Export 백필의 스레드 재조립 테스트"""

from scripts.backfill_knowledge_slack_export import group_threads

PARENT = {"ts": "100.0", "thread_ts": "100.0", "user": "U1", "text": "부모"}
REPLY = {"ts": "101.0", "thread_ts": "100.0", "user": "U2", "text": "답글"}
STANDALONE = {"ts": "200.0", "user": "U1", "text": "혼잣말"}


def test_같은_thread_ts는_한_스레드로_묶인다():
    threads, _ = group_threads([PARENT, REPLY])
    assert len(threads) == 1
    assert [m["ts"] for m in threads[0]] == ["100.0", "101.0"]


def test_스레드는_시간순으로_정렬된다():
    threads, _ = group_threads([REPLY, PARENT])
    assert [m["ts"] for m in threads[0]] == ["100.0", "101.0"]


def test_thread_ts가_없는_메시지도_스레드_하나가_된다():
    threads, _ = group_threads([STANDALONE])
    assert [m["ts"] for m in threads[0]] == ["200.0"]


def test_같은_메시지가_여러_파일에_나와도_한_번만_담는다():
    threads, _ = group_threads([PARENT, REPLY, REPLY])
    assert len(threads[0]) == 2


def test_부모가_없는_스레드는_버린다():
    threads, orphans = group_threads([REPLY])
    assert threads == []
    assert orphans == 1


def test_입퇴장은_스레드가_되지_않는다():
    join = {"ts": "300.0", "subtype": "channel_join", "user": "U1", "text": "왔다"}
    threads, _ = group_threads([join])
    assert threads == []
