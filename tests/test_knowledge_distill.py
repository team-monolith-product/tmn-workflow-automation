"""지식베이스 스레드 정제 테스트"""

import json
from unittest.mock import MagicMock

from service.knowledge.distill import (
    Distilled,
    distill_thread,
    render_distilled_text,
    store_distilled,
)

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
