"""
지식베이스에 읽기 전용 SQL을 실행하는 Service Layer입니다.

봇 도구와 MCP 서버가 같은 함수를 부릅니다. 실행 규칙이 둘로 갈리면 한쪽에서
되는 질의가 다른 쪽에서 막히고, 그 차이를 query_log로는 설명할 수 없게 됩니다.

고정된 검색 함수가 아니라 SQL을 그대로 받습니다. LIKE 한 방으로는 기간·복수
채널·집계를 표현할 수 없어서, 에이전트가 할 수 있는 것이 키워드를 바꿔가며 같은
도구를 여러 번 부르는 것뿐이었습니다.

트랜잭션을 READ ONLY로 열어 쓰기를 막습니다. 적재 파이프라인과 같은 롤로
접속하므로 권한으로는 갈리지 않습니다. query_log 적재는 쓰기라서 이 커넥션으로
할 수 없고, 따로 접속해 남깁니다.

결과는 서버 커서로 한 묶음씩 당겨오며 글자 예산이 떨어지면 멈춥니다. 다 받아
놓고 자르면 `SELECT raw_text FROM item` 한 줄에 스레드 원문 1.5만 건이 그대로
메모리에 올라옵니다.
"""

import json
import time
from datetime import date, datetime
from typing import Any, Iterable

import psycopg

from service.db import connect

# 옛 검색 도구가 스니펫 20건으로 돌려주던 분량이다.
DEFAULT_CHAR_LIMIT = 8_000
# 에이전트 문맥을 지키는 상한. 스레드 원문이 평균 1,148자라 이 값이면 원문
# 40여 건에서 멈춘다.
MAX_CHAR_LIMIT = 50_000

# 서버 커서가 한 번에 당겨올 행 수
FETCH_SIZE = 100

SCHEMA_GUIDE = """
스키마(PostgreSQL):
- data_source(id, source, external_id, name, enabled): 슬랙 채널 하나가 한 행.
  name이 "t_개발_백" 같은 채널 이름이다.
- item(id, data_source_id, external_id, url, title, author, source_created_at,
  source_updated_at, raw jsonb, raw_text, char_len, distilled jsonb,
  distilled_text, metadata jsonb, indexed_at): 슬랙 스레드 하나가 한 행.
  raw_text가 스레드 원문이고 평균 1,148자다. distilled_text는 아직 전부 비어 있다.
- query_log(actor, tool, query, filters, latency_ms, created_at): 이 도구의 실행 기록.

구글 시트 찾기:
- data_source.source='drive_sheet' 인 item 이 구글 시트 카탈로그다. **한 행이 시트
  하나**이고, raw_text 는 "시트 이름 + 탭 이름 + 머리행"이다. 셀 값은 들어 있지
  않다 -- 응답이 계속 쌓여 곧 낡기 때문이다.
- 그래서 여기서 답할 수 있는 것은 "그런 시트가 어디 있나"까지다. 행수·집계·명단은
  **execute_python 안에서 read_sheet 로 실시간으로 읽어** 처리한다.
- metadata->'tabs' 에 탭별 gid 와 columns 가 있다. 시트를 찾은 뒤
  external_id(=스프레드시트 ID)와 탭 이름(또는 gid)을 그 코드에 넘긴다.
- 예: 출석 열이 있는 시트 찾기
  SELECT i.title, i.external_id, i.url
  FROM item i JOIN data_source d ON d.id = i.data_source_id
  WHERE d.source = 'drive_sheet' AND lower(i.raw_text) LIKE lower('%출석%')
  ORDER BY i.source_updated_at DESC

규약:
- 어휘를 찾을 때는 lower(raw_text) LIKE lower('%키워드%')로 쓴다. GIN(pg_bigm)
  인덱스가 lower(raw_text)에만 걸려 있어 ILIKE나 정규식은 전체 스캔이 된다.
- raw_text를 통째로 고르면 몇 행 만에 글자 상한에 닿는다. substring()으로 맞은
  자리 주변만 자르고, url을 함께 골라 원문으로 넘긴다.
- SELECT·WITH·VALUES만 실행된다. 트랜잭션이 READ ONLY라 쓰기는 거부되고,
  세미콜론으로 문장을 이어붙일 수 없다.
""".strip()

LOG_QUERY = """
INSERT INTO query_log (actor, tool, query, filters, latency_ms)
VALUES (%(actor)s, %(tool)s, %(query)s, %(filters)s, %(latency_ms)s)
"""

# 봇 도구와 MCP 도구가 같은 설명을 씁니다. 스키마를 한쪽에만 고쳐 넣으면
# 어느 에이전트가 무엇을 알고 SQL을 썼는지가 갈립니다.
QUERY_TOOL_DESCRIPTION = f"""
사내 슬랙 공개 채널의 과거 대화가 쌓인 지식베이스에 읽기 전용 SQL을 실행합니다.
"예전에 이거 어떻게 했었지", "이 에러 본 적 있나" 같은 질문에 사용합니다.

인자:
- sql: 실행할 SQL
- char_limit: 돌려받을 글자 수 상한. 기본 {DEFAULT_CHAR_LIMIT}, 최대 {MAX_CHAR_LIMIT}.
  넘치면 잘라내고 어디서 잘렸는지 알려줍니다.

{SCHEMA_GUIDE}
""".strip()


def format_value(value: Any) -> str:
    """한 칸에 들어갈 값을 한 줄 문자열로 만듭니다.

    Args:
        value: 행에서 꺼낸 값

    Returns:
        str: 줄바꿈이 없는 문자열. NULL은 빈 칸
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, (datetime, date)):
        value = value.isoformat()
    return str(value).replace("\n", " ")


def render_rows(
    rows: Iterable[dict[str, Any]], char_limit: int
) -> tuple[str, int, bool]:
    """행을 파이프로 구분한 표로 만들되 글자 예산까지만 만듭니다.

    행을 하나씩 받아 예산을 넘기는 순간 멈춥니다. 호출부가 서버 커서를 넘기므로,
    여기서 멈추면 남은 행은 아예 당겨오지 않습니다.

    Args:
        rows: 행 목록. dict 하나가 한 행
        char_limit: 돌려줄 글자 수 상한

    Returns:
        tuple[str, int, bool]: 표, 읽은 행 수, 잘렸는지 여부
    """
    lines: list[str] = []
    length = 0
    row_count = 0

    for row in rows:
        if not lines:
            header = " | ".join(row.keys())
            lines.append(header)
            length += len(header)

        line = " | ".join(format_value(value) for value in row.values())
        lines.append(line)
        length += len(line) + 1
        row_count += 1

        if length > char_limit:
            rendered = "\n".join(lines)[:char_limit]
            return (
                f"{rendered}\n"
                f"…{char_limit}자에서 잘렸습니다({row_count}행까지 읽음). "
                "LIMIT을 줄이거나 substring()으로 컬럼을 좁히세요.",
                row_count,
                True,
            )

    if not lines:
        return "결과가 없습니다.", 0, False

    return "\n".join(lines), row_count, False


def run_query(
    sql: str, actor: str, tool: str, char_limit: int = DEFAULT_CHAR_LIMIT
) -> str:
    """읽기 전용으로 SQL을 실행하고 결과를 표로 돌려줍니다.

    실패를 예외가 아니라 문자열로 돌려줍니다. 부르는 쪽이 전부 에이전트 도구라
    메시지를 받아 SQL을 고쳐 다시 부르는 것이 유일한 처리이기 때문입니다.

    질의를 query_log에 남기는 것이 이 함수의 책임입니다. 호출부에 맡기면
    빠뜨린 경로가 생기고, 무엇이 검색되지 않는지는 이 표로만 알 수 있습니다.
    임의 SQL은 무엇이 나올지 정해져 있지 않아 result_ids는 채우지 않습니다.

    Args:
        sql: 실행할 SQL
        actor: 질의한 사람의 이메일
        tool: 질의가 들어온 경로. "slack", "mcp", "route_bug"
        char_limit: 돌려받을 글자 수 상한. MAX_CHAR_LIMIT까지

    Returns:
        str: 파이프로 구분한 표. 실패하면 그 이유
    """
    budget = min(char_limit, MAX_CHAR_LIMIT)
    filters: dict[str, Any] = {"char_limit": budget}

    started = time.monotonic()
    try:
        with connect(read_only=True) as conn:
            # 서버 커서는 DECLARE ... CURSOR FOR를 타므로 SELECT·WITH·VALUES가
            # 아닌 문장과 세미콜론으로 이어붙인 여러 문장이 여기서 걸린다.
            with conn.cursor(name="knowledge_query") as cur:
                cur.itersize = FETCH_SIZE
                cur.execute(sql)
                rendered, row_count, truncated = render_rows(cur, budget)
        filters |= {"rows": row_count, "truncated": truncated}
    except psycopg.Error as error:
        rendered = f"질의가 실패했습니다: {error}"
        filters |= {"error": str(error).strip()}

    _log_query(actor, tool, sql, filters, int((time.monotonic() - started) * 1000))
    return rendered


def _log_query(
    actor: str, tool: str, sql: str, filters: dict[str, Any], latency_ms: int
) -> None:
    """질의 기록을 남깁니다. 읽기 전용 커넥션으로는 쓸 수 없어 따로 접속합니다."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            LOG_QUERY,
            {
                "actor": actor,
                "tool": tool,
                "query": sql,
                "filters": json.dumps(filters, ensure_ascii=False),
                "latency_ms": latency_ms,
            },
        )
