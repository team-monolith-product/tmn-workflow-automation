import json
import time
from datetime import date, datetime
from typing import Any, Iterable

import psycopg

from service.db import connect

DEFAULT_CHAR_LIMIT = 8_000
MAX_CHAR_LIMIT = 50_000

FETCH_SIZE = 100

SCHEMA_GUIDE = """
스키마(PostgreSQL):
- data_source(id, source, external_id, name, enabled): 데이터 출처.
  source는 출처 유형, external_id는 해당 출처의 식별자.
- item(id, data_source_id, external_id, url, title, author, source_created_at,
  source_updated_at, raw jsonb, raw_text, char_len, distilled jsonb,
  distilled_text, metadata jsonb, indexed_at): 수집 항목.
  slack은 스레드 단위이며 raw_text에 원문이 있다.
  drive_sheet는 시트 단위이며 raw_text에 시트명·탭명·머리행이 있다.
  metadata->'tabs'에는 탭별 gid·columns가 있다. 셀 값은 read_sheet로 조회한다.
- query_log(actor, tool, query, filters, latency_ms, created_at): SQL 실행 기록.
- sms_log(id, ref_key, message_key, channel_id, project, thread_ts, sender,
  content, message_type, approved_by, sent_at, scheduled_at, phone, name,
  change_word jsonb): 수신자별 문자 발송 기록. ref_key는 발송 식별자.
  sent_at은 발송 접수 시각, scheduled_at은 예약 발송 시각(timestamptz, 세션 UTC).
  content는 치환 전 문안, change_word의 var1~var8은 [*1*]~[*8*] 치환값.
  name은 미사용 시 NULL, 치환값 누락 시 빈 문자열. phone은 하이픈 없는 번호.
  project는 발송 당시 사업명. channel_id는 슬랙 채널 ID.
- revenue_transactions(year, issued_on, status, customer, item, quantity, unit_price,
  amount, tax, total, category, subcategory, note, school_level, edu_office,
  budget_sources text[], terms text[], program_name, program_client,
  program_budget, program_our_revenue, program_stage): 매출장 한 행당 한 거래.
  year는 탭의 귀속연도, issued_on은 발행일. status는 issued(발행)·planned(예정).
  amount는 공급가액, tax는 세액, total은 세금 포함 금액.
  category·subcategory는 상품 분류. school_level·edu_office·budget_sources·terms는
  고객 단위 CRM 정보이며, program_*은 사업 단위 CRM 정보다.
  같은 고객·사업의 정보가 여러 거래에 반복된다. 미매칭 필드는 NULL이다.

실행: SELECT·WITH·VALUES 단일 문장, 읽기 전용.
검색 인덱스: lower(item.raw_text)의 GIN(pg_bigm).
""".strip()


LOG_QUERY = """
INSERT INTO query_log (actor, tool, query, filters, latency_ms)
VALUES (%(actor)s, %(tool)s, %(query)s, %(filters)s, %(latency_ms)s)
"""

QUERY_TOOL_DESCRIPTION = f"""
사내 지식베이스의 슬랙 대화·매출 등에 읽기 전용 SQL을 실행합니다.

인자:
- sql: 실행할 SQL
- char_limit: 돌려받을 글자 수 상한. 기본 {DEFAULT_CHAR_LIMIT}, 최대 {MAX_CHAR_LIMIT}.
  초과 시 결과가 잘립니다.

{SCHEMA_GUIDE}
""".strip()


def format_value(value: Any) -> str:
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
                f"…{char_limit}자에서 잘렸습니다({row_count}행까지 읽음).",
                row_count,
                True,
            )

    if not lines:
        return "결과가 없습니다.", 0, False

    return "\n".join(lines), row_count, False


def run_query(
    sql: str, actor: str, tool: str, char_limit: int = DEFAULT_CHAR_LIMIT
) -> str:
    budget = min(char_limit, MAX_CHAR_LIMIT)
    filters: dict[str, Any] = {"char_limit": budget}

    started = time.monotonic()
    try:
        with connect(read_only=True) as conn:
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
