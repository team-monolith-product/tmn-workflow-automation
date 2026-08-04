"""
스레드 원문을 LLM으로 재작성하는 Service Layer입니다.

어휘 검색은 raw_text를 봅니다. 조사가 붙거나 같은 것을 다른 말로 적으면 걸리지
않습니다. 정제문은 그 반대편을 맡습니다. question을 "엔지니어가 실제로 검색할
법한 한 줄"로 뽑으므로 사용자 질의와 같은 어휘 공간에 놓입니다.

결과를 distilled(구조)와 distilled_text(이어붙인 문서) 양쪽에 남깁니다. 항목
조합을 바꾸고 싶을 때 LLM을 다시 돌리지 않고 다시 렌더하기만 하면 됩니다.
정제가 파이프라인에서 제일 비쌉니다.
"""

import json
from typing import Any

import psycopg
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from service.llm import DEFAULT_MODEL

# 정제 대상은 distill_after를 지난 pending이다. 백필한 물량이 전부 여기 걸려
# 있어 최신순으로 꺼낸다. 며칠에 걸쳐 흘리는 동안 사람들이 실제로 찾는 것은
# 최근 스레드다. distill_after는 마지막 활동 시각에서 일정 시간을 더한 값이라
# 이 정렬이 곧 최신순이고, item_distill_q 인덱스를 그대로 쓴다.
CLAIM_PENDING = """
SELECT id, raw_text, content_hash
FROM item
WHERE distill_state = 'pending' AND distill_after <= now()
ORDER BY distill_after DESC
LIMIT %(limit)s
"""

# content_hash를 조건에 두는 이유가 있다. 정제하는 사이에 답글이 달리면 수집
# 경로가 raw_text를 갈아끼우고 상태를 pending으로 되돌린다. 그때 낡은 결과를
# done으로 덮으면 새 내용이 영영 정제되지 않는다. 안 맞으면 0행이 갱신되고
# 그 행은 pending으로 남아 다음 회차에 다시 잡힌다.
STORE_DISTILLED = """
UPDATE item
SET distilled      = %(distilled)s,
    distilled_text = %(distilled_text)s,
    distill_state  = 'done'
WHERE id = %(id)s AND content_hash = %(content_hash)s
"""

# 한 건이 계속 실패해도 큐를 막지 않도록 상태를 옮긴다. 이유는 metadata에
# 남긴다. 따로 컬럼을 만들 만큼 자주 볼 값이 아니다.
MARK_ERROR = """
UPDATE item
SET distill_state = 'error',
    metadata      = metadata || jsonb_build_object('distill_error', %(reason)s::text)
WHERE id = %(id)s
"""

PROMPT = """\
사내 슬랙 스레드다. 나중에 검색해서 찾아 읽을 사람을 위해 재작성하라.

- question: 이 스레드를 찾으려는 사람이 검색창에 칠 법한 한 줄. 물음표로 끝나는
  질문 형태로 쓴다
- summary: 무슨 상황이었는지 두세 문장
- resolution: 어떻게 됐는지. 결론이 안 난 스레드면 안 났다고 쓴다
- systems: 언급된 서비스·저장소·인프라 이름
- code_refs: 언급된 파일 경로, 함수명, 에러 코드, 리소스 식별자

원문에 없는 것을 지어내지 않는다. 해당 항목이 없으면 빈 값으로 둔다.
한국어로 쓴다. 고유명사와 식별자는 원문 표기 그대로 옮긴다.

---

{raw_text}"""


class Distilled(BaseModel):
    """스레드 하나를 재작성한 결과."""

    question: str = Field(description="검색창에 칠 법한 한 줄 질문")
    summary: str = Field(description="상황 요약 두세 문장")
    resolution: str = Field(description="결론. 안 났으면 안 났다고 쓴다")
    systems: list[str] = Field(description="언급된 서비스·저장소·인프라 이름")
    code_refs: list[str] = Field(description="파일 경로·함수명·에러 코드·리소스 ID")


def render_distilled_text(distilled: Distilled) -> str:
    """정제 결과를 검색 대상 평문으로 이어붙입니다.

    Args:
        distilled: distill_thread 결과

    Returns:
        str: 항목별 한 줄. 빈 항목은 넣지 않는다
    """
    lines = [
        f"질문: {distilled.question}",
        f"요약: {distilled.summary}",
        f"해결: {distilled.resolution}",
    ]
    if distilled.systems:
        lines.append(f"시스템: {', '.join(distilled.systems)}")
    if distilled.code_refs:
        lines.append(f"코드: {', '.join(distilled.code_refs)}")
    return "\n".join(lines)


def distill_thread(raw_text: str, llm=None) -> Distilled:
    """스레드 원문 하나를 재작성합니다.

    Args:
        raw_text: build_raw_text가 만든 스레드 평문
        llm: 구조화 출력이 설정된 클라이언트. 생략하면 기본 모델로 만든다

    Returns:
        Distilled: 재작성 결과
    """
    client = llm or ChatOpenAI(
        model=DEFAULT_MODEL, temperature=0
    ).with_structured_output(Distilled)
    return client.invoke(PROMPT.format(raw_text=raw_text))


def claim_pending(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    """정제할 차례가 된 스레드를 꺼냅니다.

    Args:
        conn: 커넥션
        limit: 최대 건수

    Returns:
        list[dict[str, Any]]: id, raw_text, content_hash
    """
    with conn.cursor() as cur:
        cur.execute(CLAIM_PENDING, {"limit": limit})
        return cur.fetchall()


def store_distilled(
    conn: psycopg.Connection, item_id: int, content_hash: str, distilled: Distilled
) -> bool:
    """정제 결과를 저장합니다.

    Args:
        conn: 커넥션
        item_id: item.id
        content_hash: 정제를 시작할 때 읽은 해시
        distilled: distill_thread 결과

    Returns:
        bool: 저장했으면 True. 그사이 내용이 바뀌었으면 False
    """
    with conn.cursor() as cur:
        cur.execute(
            STORE_DISTILLED,
            {
                "id": item_id,
                "content_hash": content_hash,
                "distilled": json.dumps(distilled.model_dump(), ensure_ascii=False),
                "distilled_text": render_distilled_text(distilled),
            },
        )
        return cur.rowcount == 1


def mark_error(conn: psycopg.Connection, item_id: int, reason: str) -> None:
    """정제에 실패한 스레드를 error로 옮깁니다.

    Args:
        conn: 커넥션
        item_id: item.id
        reason: 실패 사유
    """
    with conn.cursor() as cur:
        cur.execute(MARK_ERROR, {"id": item_id, "reason": reason})
