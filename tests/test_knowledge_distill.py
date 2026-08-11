"""지식베이스 스레드 정제 테스트"""

import contextlib
import json
from unittest.mock import MagicMock

import pytest

import scripts.distill_knowledge_items as worker
from service.knowledge import distill
from service.knowledge.distill import (
    Distilled,
    distill_thread,
    render_distilled_text,
    store_distilled,
)


@contextlib.contextmanager
def _ctx(conn):
    """connect() 가 돌려주는 컨텍스트 매니저를 흉내냅니다."""
    yield conn


DISTILLED = Distilled(
    question="Sidekiq 큐가 밀릴 때 어디부터 보나요?",
    summary="야간 배치가 몰려 큐가 30분 밀렸다.",
    resolution="워커 수를 늘려 해소했다.",
    systems=["class-rails", "Valkey"],
    code_refs=["app/workers/report_worker.rb"],
)


def test_정제문은_항목마다_한_줄이다():
    rendered = render_distilled_text(DISTILLED)

    assert rendered.splitlines() == [
        "질문: Sidekiq 큐가 밀릴 때 어디부터 보나요?",
        "요약: 야간 배치가 몰려 큐가 30분 밀렸다.",
        "해결: 워커 수를 늘려 해소했다.",
        "시스템: class-rails, Valkey",
        "코드: app/workers/report_worker.rb",
    ]


def test_빈_목록은_줄을_만들지_않는다():
    rendered = render_distilled_text(
        DISTILLED.model_copy(update={"systems": [], "code_refs": []})
    )

    assert "시스템:" not in rendered
    assert "코드:" not in rendered


def test_원문을_그대로_프롬프트에_넣는다():
    llm = MagicMock()
    llm.invoke.return_value = DISTILLED

    assert distill_thread("lch@team-mono.com: 큐가 밀립니다", llm=llm) is DISTILLED
    assert "lch@team-mono.com: 큐가 밀립니다" in llm.invoke.call_args[0][0]


def test_저장은_읽어둔_해시가_그대로일_때만_한다():
    # 정제하는 사이 답글이 달리면 수집 경로가 raw_text를 갈아끼우고 상태를
    # pending으로 되돌린다. 낡은 결과를 done으로 덮으면 새 내용이 영영
    # 정제되지 않는다.
    cursor = MagicMock()
    cursor.rowcount = 0
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    assert store_distilled(conn, 7, "낡은해시", DISTILLED) is False
    assert cursor.execute.call_args[0][1]["content_hash"] == "낡은해시"


def test_구조와_평문을_함께_남긴다():
    # 항목 조합을 바꿀 때 LLM을 다시 돌리지 않고 다시 렌더하려면 구조가 남아야
    # 한다.
    cursor = MagicMock()
    cursor.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    assert store_distilled(conn, 7, "해시", DISTILLED) is True

    parameters = cursor.execute.call_args[0][1]
    assert json.loads(parameters["distilled"])["question"] == DISTILLED.question
    assert parameters["distilled_text"] == render_distilled_text(DISTILLED)


def test_저장할_때_프롬프트_버전을_함께_박는다():
    # 이게 없으면 프롬프트를 고쳐도 이미 done 인 것은 구 결과로 남고, 같은
    # 컬럼에 두 세대가 섞인 채 구분할 방법이 없다.
    cursor = MagicMock()
    cursor.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    store_distilled(conn, 7, "해시", DISTILLED)

    parameters = cursor.execute.call_args[0][1]
    assert json.loads(parameters["distilled"])["_v"] == distill.PROMPT_VERSION


def test_원문이_길면_잘라서_넣는다():
    # 초대형 스레드가 컨텍스트를 넘겨 영구 실패하는 것보다 일부 반영이 낫다.
    llm = MagicMock()
    llm.invoke.return_value = DISTILLED

    distill_thread("가" * (distill.MAX_RAW_CHARS + 500), llm=llm)

    sent = llm.invoke.call_args[0][0]
    assert len(sent) == len(distill.PROMPT.format(raw_text="")) + distill.MAX_RAW_CHARS


def test_구조화_출력이_비면_성공으로_보지_않는다():
    # None 을 그대로 돌려주면 호출부가 성공으로 보고 저장 단계에서 배치가 죽는다.
    llm = MagicMock()
    llm.invoke.return_value = None

    with pytest.raises(ValueError):
        distill_thread("원문", llm=llm)


def _conn_returning(state: str):
    cursor = MagicMock()
    cursor.rowcount = 1
    cursor.fetchone.return_value = {"distill_state": state, "ok": True}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


def test_실패_처리에_재시도_한도를_넘긴다():
    # 상태 전이 자체는 SQL 이 정하므로 mock 으로는 확인할 수 없다.
    # 그건 tests/test_knowledge_distill_sql.py 가 실제 Postgres 로 본다.
    conn, cursor = _conn_returning("pending")

    distill.mark_error(conn, 7, "429")

    assert cursor.execute.call_args[0][1]["max_attempts"] == distill.MAX_ATTEMPTS


def _wire(monkeypatch, conn, items):
    monkeypatch.setattr(worker, "connect", lambda _dsn: _ctx(conn))
    monkeypatch.setattr(worker, "acquire_lock", lambda _conn: True)
    monkeypatch.setattr(worker, "count_pending", lambda _conn: len(items))
    monkeypatch.setattr(worker, "fetch_pending", lambda _conn, _limit: items)
    monkeypatch.setattr(worker, "build_client", lambda: MagicMock())


def test_한_건도_저장_못한_회차는_상태를_옮기지_않고_죽는다(monkeypatch):
    # 전부 실패는 개별 스레드 문제가 아니라 프롬프트·키 사고다. 그대로 error 를
    # 찍으면 회차마다 limit 건씩 굳어 이틀이면 큐 전체가 죽는다.
    items = [
        {"id": 1, "raw_text": "가", "content_hash": "h1"},
        {"id": 2, "raw_text": "나", "content_hash": "h2"},
    ]
    conn, _ = _conn_returning("pending")
    _wire(monkeypatch, conn, items)
    monkeypatch.setattr(
        worker, "distill_thread", MagicMock(side_effect=RuntimeError("키 만료"))
    )
    marked = MagicMock()
    monkeypatch.setattr(worker, "mark_error", marked)

    with pytest.raises(RuntimeError, match="전부 실패"):
        worker.distill_batch(2, 1, None, dry_run=False)

    marked.assert_not_called()


def test_한_건이_실패해도_나머지는_저장한다(monkeypatch):
    items = [
        {"id": 1, "raw_text": "가", "content_hash": "h1"},
        {"id": 2, "raw_text": "나", "content_hash": "h2"},
    ]
    conn, _ = _conn_returning("pending")
    _wire(monkeypatch, conn, items)

    def flaky(raw_text, _client):
        if raw_text == "가":
            raise RuntimeError("일시 오류")
        return DISTILLED

    monkeypatch.setattr(worker, "distill_thread", flaky)
    monkeypatch.setattr(worker, "mark_error", MagicMock(return_value="pending"))
    stored = MagicMock(return_value=True)
    monkeypatch.setattr(worker, "store_distilled", stored)

    worker.distill_batch(2, 1, None, dry_run=False)

    assert stored.call_count == 1


def test_다른_회차가_돌면_건너뛴다(monkeypatch):
    # claim_pending 이 SELECT 뿐이라 이 잠금이 유일한 중복 처리 차단 장치다.
    conn, _ = _conn_returning("pending")
    monkeypatch.setattr(worker, "connect", lambda _dsn: _ctx(conn))
    monkeypatch.setattr(worker, "acquire_lock", lambda _conn: False)
    fetched = MagicMock()
    monkeypatch.setattr(worker, "fetch_pending", fetched)

    worker.distill_batch(50, 4, None, dry_run=False)

    fetched.assert_not_called()
