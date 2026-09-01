"""Slack List 작업을 공용 작업 스레드와 연결합니다.

에이전트의 작업 중 대화는 Slack으로 복사하지 않습니다. 이 모듈은 작업 시작 때
결과를 남길 루트를 연결하고, 실제 작업이 끝났을 때 선별한 결과와 경험 한 건을
게시하는 두 동작만 제공합니다.
"""

import asyncio
import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import psycopg
from slack_sdk.errors import SlackApiError, SlackRequestError
from slack_sdk.web.async_client import AsyncWebClient

from service.db import get_dsn
from service.slack_task_list import build_completion_cells
from service.slack_task_message import (
    SlackMessageLocation,
    find_message_ts,
    get_permalink as _permalink,
    message_location,
    result_client_msg_id,
)

SOURCE_THREAD_COLUMN_NAME = "요청 맥락"
SOURCE_THREAD_COLUMN_KEY = "slack_thread"
WORK_THREAD_COLUMN_NAME = "작업 기록"
WORK_THREAD_COLUMN_KEY = "work_thread"
MAX_THREAD_PAGES = 5
THREAD_PAGE_SIZE = 100
MAX_THREAD_CHARS = 20_000

TaskResultStatus = Literal["completed", "blocked", "handoff"]

STATUS_LABELS: dict[TaskResultStatus, str] = {
    "completed": "완료",
    "blocked": "막힘",
    "handoff": "인계",
}

SECRET_PATTERN = re.compile(
    r"(?:xox[baprs]-\S+|sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"AKIA[0-9A-Z]{16}|Bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
LOCAL_PATH_PATTERN = re.compile(
    r"(?:^|[\s(])(?:/Users/|/home/|file://|[A-Za-z]:\\)", re.MULTILINE
)
OUTPUT_PATTERN = re.compile(r"^(.+?):\s*(https://[^\s<>]+)$", re.IGNORECASE)
MAX_REFERENCE_COUNT = 30
SLACK_SECTION_TEXT_LIMIT = 3_000


@dataclass(frozen=True)
class SlackListTaskReference:
    """Slack List URL에서 읽은 작업 식별자입니다."""

    list_url: str
    list_id: str
    record_id: str


def _read_cell(
    item: dict[str, Any], column_id: str | None, value_key: str
) -> Any | None:
    """Slack List 행에서 지정한 열 값을 읽습니다."""
    if not column_id:
        return None
    for field in item.get("fields", []):
        if field.get("column_id") == column_id:
            return field.get(value_key)
    return None


@dataclass(frozen=True)
class SlackTaskListSchema:
    """items.info가 돌려준 작업 List의 열 계약입니다."""

    name_column_id: str
    completed_column_id: str
    assignee_column_id: str
    due_date_column_id: str
    source_thread_column_id: str | None
    work_thread_column_id: str

    def title_of(self, record: dict[str, Any]) -> str:
        return _read_cell(record, self.name_column_id, "text") or ""

    def is_completed(self, record: dict[str, Any]) -> bool:
        values = _read_cell(record, self.completed_column_id, "checkbox")
        return bool(values and values[0])

    def assignees_of(self, record: dict[str, Any]) -> list[str]:
        return _read_cell(record, self.assignee_column_id, "user") or []

    def due_dates_of(self, record: dict[str, Any]) -> list[str]:
        return _read_cell(record, self.due_date_column_id, "date") or []

    def source_thread_references_of(
        self, record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return self._message_references_of(record, self.source_thread_column_id)

    def work_thread_references_of(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return self._message_references_of(record, self.work_thread_column_id)

    @staticmethod
    def _message_references_of(
        record: dict[str, Any], column_id: str | None
    ) -> list[dict[str, Any]]:
        raw = _read_cell(record, column_id, "message")
        if raw is None:
            return []
        values = raw if isinstance(raw, list) else [raw]

        normalized = []
        for value in values:
            if isinstance(value, str):
                normalized.append({"value": value})
            elif isinstance(value, dict):
                normalized.append(value)
            else:
                raise ValueError("Slack message 셀의 형식을 해석할 수 없습니다.")
        return normalized


def parse_slack_list_task_url(list_url: str) -> SlackListTaskReference:
    """Slack List의 record URL을 검증하고 ID를 꺼냅니다.

    Args:
        list_url: ``.../lists/<team>/<list>?record_id=<record>`` 형태 URL

    Returns:
        SlackListTaskReference: List와 record ID
    """
    parsed = urlparse(list_url.strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname == "slack.com" or hostname.endswith(".slack.com")
    ):
        raise ValueError("https Slack List 링크를 사용해주세요.")

    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 3 or segments[0] != "lists":
        raise ValueError("Slack List의 작업 행 링크가 아닙니다.")

    list_id = segments[2]
    record_ids = parse_qs(parsed.query).get("record_id", [])
    if not re.fullmatch(r"F[A-Z0-9]+", list_id) or len(record_ids) != 1:
        raise ValueError("Slack List 링크에 올바른 list_id와 record_id가 필요합니다.")

    record_id = record_ids[0]
    if not re.fullmatch(r"Rec[A-Z0-9]+", record_id):
        raise ValueError("Slack List 링크의 record_id 형식이 올바르지 않습니다.")

    return SlackListTaskReference(
        list_url=list_url.strip(), list_id=list_id, record_id=record_id
    )


def _find_message_column_id(
    schema: list[dict[str, Any]], key: str, name: str
) -> str | None:
    """List 스키마에서 key 또는 표시 이름이 맞는 message 열을 고릅니다."""
    candidates = [
        column
        for column in schema
        if column.get("type") == "message"
        and (column.get("key") == key or str(column.get("name", "")).strip() == name)
    ]
    if len(candidates) > 1:
        raise ValueError(
            f'message 타입의 "{name}" 열이 여러 개입니다. 하나만 남겨주세요.'
        )
    if not candidates:
        return None
    return str(candidates[0]["id"])


def find_work_thread_column_id(schema: list[dict[str, Any]]) -> str:
    """List 스키마에서 작업 기록 message 열을 하나만 고릅니다.

    Args:
        schema: ``list_metadata.schema``

    Returns:
        str: 작업 기록 열 ID
    """
    column_id = _find_message_column_id(
        schema, WORK_THREAD_COLUMN_KEY, WORK_THREAD_COLUMN_NAME
    )
    if column_id is None:
        raise ValueError(
            f'이 List에 message 타입의 "{WORK_THREAD_COLUMN_NAME}" 열을 먼저 추가해주세요.'
        )
    return column_id


def task_list_schema(schema: list[dict[str, Any]]) -> SlackTaskListSchema:
    """items.info의 List 스키마를 작업 행을 읽는 열 계약으로 바꿉니다."""
    columns: dict[str, str] = {}
    for column in schema:
        column_id = column.get("id")
        key = "name" if column.get("is_primary_column") else column.get("key")
        if column_id and key:
            columns[str(key)] = str(column_id)
    required = {
        "name",
        "todo_completed",
        "todo_assignee",
        "todo_due_date",
    }
    missing = required - columns.keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"Slack List 작업 열이 부족합니다: {names}")

    return SlackTaskListSchema(
        name_column_id=columns["name"],
        completed_column_id=columns["todo_completed"],
        assignee_column_id=columns["todo_assignee"],
        due_date_column_id=columns["todo_due_date"],
        source_thread_column_id=_find_message_column_id(
            schema,
            SOURCE_THREAD_COLUMN_KEY,
            SOURCE_THREAD_COLUMN_NAME,
        ),
        work_thread_column_id=find_work_thread_column_id(schema),
    )


def acquire_task_record_lock(
    reference: SlackListTaskReference,
) -> psycopg.Connection:
    """작업 루트 중복 생성을 막는 세션 advisory lock을 잡습니다."""
    conn = psycopg.connect(get_dsn())
    lock_key = f"slack-list-task:{reference.list_id}:{reference.record_id}"
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,))
    return conn


def release_task_record_lock(conn: psycopg.Connection) -> None:
    """연결을 닫아 세션 advisory lock을 해제합니다."""
    conn.close()


async def _read_record(
    client: AsyncWebClient,
    reference: SlackListTaskReference,
) -> tuple[SlackTaskListSchema, dict[str, Any]]:
    try:
        response = await client.slackLists_items_info(
            list_id=reference.list_id, id=reference.record_id
        )
    except SlackApiError as exc:
        error = exc.response.get("error", "slack_api_error")
        raise ValueError(
            f"Slack List 작업 항목을 읽지 못했습니다({error}). 항목이 삭제되었거나 "
            "다른 List로 이동했을 수 있습니다."
        ) from exc
    schema = task_list_schema(response["list"]["list_metadata"]["schema"])
    return schema, response["record"]


async def _read_thread(
    client: AsyncWebClient, reference: dict[str, Any]
) -> dict[str, Any]:
    location = message_location(reference)
    messages: list[dict[str, str]] = []
    cursor = None
    char_count = 0
    truncated = False

    for _ in range(MAX_THREAD_PAGES):
        response = await client.conversations_replies(
            channel=location.channel_id,
            ts=location.root_ts,
            limit=THREAD_PAGE_SIZE,
            cursor=cursor,
        )
        for message in response.get("messages", []):
            text = str(message.get("text", ""))
            remaining = MAX_THREAD_CHARS - char_count
            if remaining <= 0:
                truncated = True
                break
            if len(text) > remaining:
                text = text[:remaining] + "…"
                truncated = True

            messages.append(
                {
                    "author": str(
                        message.get("user")
                        or message.get("bot_id")
                        or message.get("username")
                        or "unknown"
                    ),
                    "ts": str(message.get("ts", "")),
                    "text": text,
                }
            )
            char_count += len(text)
            if truncated:
                break

        if truncated:
            break
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    else:
        truncated = True

    return {
        "permalink": await _permalink(client, location, root=True),
        "messages": messages,
        "truncated": truncated,
    }


async def _read_source_thread(
    client: AsyncWebClient, reference: dict[str, Any]
) -> dict[str, Any]:
    """삭제·권한 오류가 난 요청 맥락은 이유를 남기고 빈 대화로 돌려줍니다."""
    try:
        return await _read_thread(client, reference)
    except SlackApiError as exc:
        error = exc.response.get("error", "slack_api_error")
        detail = f"Slack 요청 맥락을 읽지 못했습니다: {error}"
    except ValueError as exc:
        detail = str(exc)

    return {
        "permalink": reference.get("value") or reference.get("permalink"),
        "messages": [],
        "truncated": False,
        "error": detail,
    }


def _work_thread_channel(source_references: list[dict[str, Any]]) -> str:
    """새 작업 스레드를 만들 첫 번째 유효한 요청 맥락의 채널을 고릅니다."""
    for source_reference in source_references:
        try:
            return message_location(source_reference).channel_id
        except ValueError:
            continue
    raise ValueError(
        "작업 기록을 새로 만들려면 요청 맥락 열에 읽을 수 있는 Slack 메시지 링크가 "
        "하나 이상 있어야 합니다."
    )


def _escape(value: str) -> str:
    return html.escape(value, quote=False)


def _slack_link(url: str, label: str) -> str:
    return f"<{_escape(url)}|{_escape(label)}>"


def _start_message(list_url: str, title: str) -> str:
    """목록 링크만 보아도 작업 시작 알림임을 알 수 있게 만듭니다."""
    return f"작업을 시작합니다.\n• 작업: {_slack_link(list_url, title)}"


async def _actor_mention(client: AsyncWebClient, actor: str) -> str:
    """인증된 이메일을 Slack 멘션으로 바꾸고, 찾지 못하면 이메일을 남깁니다."""
    try:
        user = (await client.users_lookupByEmail(email=actor)).get("user") or {}
    except SlackApiError:
        return _escape(actor)

    user_id = user.get("id")
    return f"<@{user_id}>" if user_id else _escape(actor)


def _start_reply_message(actor: str, started_at: str) -> str:
    """작업 시작 주체와 시각만 첫 댓글에 남깁니다."""
    epoch = started_at.partition(".")[0]
    slack_date = f"<!date^{epoch}^{{date_short_pretty}} {{time}}|{_escape(started_at)}>"
    return f"• 시작한 사람: {actor}\n" f"• 시작 시각: {slack_date}"


async def start_task_from_slack_list(
    client: AsyncWebClient, list_url: str, actor: str
) -> str:
    """List 행의 맥락을 읽고 공용 작업 스레드를 만들거나 재사용합니다."""
    reference = parse_slack_list_task_url(list_url)
    task_schema, record = await _read_record(client, reference)
    work_references = task_schema.work_thread_references_of(record)
    if len(work_references) > 1:
        raise ValueError("작업 기록 셀에는 Slack 스레드 링크가 하나만 있어야 합니다.")

    created = False
    if not work_references:
        lock = await asyncio.to_thread(acquire_task_record_lock, reference)
        try:
            task_schema, record = await _read_record(client, reference)
            work_references = task_schema.work_thread_references_of(record)
            if len(work_references) > 1:
                raise ValueError(
                    "작업 기록 셀에는 Slack 스레드 링크가 하나만 있어야 합니다."
                )

            if not work_references:
                source_references = task_schema.source_thread_references_of(record)
                channel_id = _work_thread_channel(source_references)
                posted = await client.chat_postMessage(
                    channel=channel_id,
                    text=_start_message(
                        reference.list_url, task_schema.title_of(record)
                    ),
                )
                root_location = SlackMessageLocation(
                    channel_id=str(posted.get("channel", channel_id)),
                    ts=str(posted["ts"]),
                    root_ts=str(posted["ts"]),
                )
                await client.chat_postMessage(
                    channel=root_location.channel_id,
                    thread_ts=root_location.root_ts,
                    text=_start_reply_message(
                        await _actor_mention(client, actor),
                        root_location.ts,
                    ),
                )
                permalink = await _permalink(client, root_location, root=True)
                await client.slackLists_items_update(
                    list_id=reference.list_id,
                    cells=[
                        {
                            "row_id": reference.record_id,
                            "column_id": task_schema.work_thread_column_id,
                            "message": [permalink],
                        }
                    ],
                )
                work_references = [
                    {
                        "value": permalink,
                        "channel_id": root_location.channel_id,
                        "ts": root_location.ts,
                    }
                ]
                created = True
        finally:
            await asyncio.to_thread(release_task_record_lock, lock)

    source_references = task_schema.source_thread_references_of(record)
    histories = await asyncio.gather(
        *(_read_source_thread(client, item) for item in source_references),
        _read_thread(client, work_references[0]),
    )
    source_threads = list(histories[:-1])
    work_thread = histories[-1]

    result = {
        "task": {
            "list_url": reference.list_url,
            "list_id": reference.list_id,
            "record_id": reference.record_id,
            "title": task_schema.title_of(record),
            "assignees": task_schema.assignees_of(record),
            "due_dates": task_schema.due_dates_of(record),
            "completed": task_schema.is_completed(record),
        },
        "source_threads": source_threads,
        "work_thread": work_thread,
        "work_thread_created": created,
        "execution_requirements": {
            "knowledge_query_before_work": True,
            "knowledge_tool": "query_knowledge",
            "knowledge_search_angles": [
                "같은 사업과 동일 업무",
                "유사 사업과 업무 패턴",
                "관련 요구사항, 결정, 실패 사례",
            ],
            "broaden_search_until_results_repeat": True,
            "repeat_search_on_material_change": True,
            "material_change_triggers": [
                "추가 요구사항 또는 예외",
                "범위, 방향, 대상, 산출물, 일정 변경",
                "새로운 고유 명사, 자료, 제약",
                "기존 판단과 충돌하는 정보",
            ],
            "coverage_goal": "서로 다른 관련 출처에서 새로운 사실이나 판단이 더 나오지 않을 때까지",
            "record_references_after_each_gate": True,
            "reference_tool": "record_slack_task_references",
            "ask_starter_if_no_references": True,
            "clarify_ambiguity_before_work": True,
            "runtime_metadata_at_finish": [
                "tool",
                "model",
                "reasoning_effort",
                "token_usage",
                "elapsed_time",
                "conversation_turns",
            ],
            "runtime_metadata_only_if_verified": True,
            "starter": actor,
        },
        "recording_rule": (
            "실제 작업이 완료되거나 막힘·인계로 종료될 때만 결과를 한 번 게시합니다. "
            "요약에는 작업 과정보다 중요한 현재 상태와 다음 행동을 쓰고, 산출물은 "
            "'산출물 이름: https://공유링크' 형식으로 남깁니다. 비슷한 사업에서도 쓸 "
            "중요한 결정·이유·요구사항·실수 방지 규칙은 산출물과 중복돼도 남기고, "
            "판단이나 행동을 바꾸지 않는 작업 과정과 통상적인 검증은 생략합니다."
        ),
    }
    return json.dumps(result, ensure_ascii=False)


def _clean_list(name: str, values: list[str] | None, maximum: int) -> list[str]:
    cleaned = [value.strip() for value in (values or []) if value.strip()]
    if len(cleaned) > maximum:
        raise ValueError(f"{name}은 최대 {maximum}개만 남겨주세요.")
    if any(len(value) > 600 for value in cleaned):
        raise ValueError(f"{name}의 각 항목은 600자 이내로 요약해주세요.")
    return cleaned


def _clean_outputs(values: list[str]) -> list[tuple[str, str]]:
    """`산출물 이름: https://링크` 입력을 Slack 이름 링크로 쓸 형태로 검증합니다."""
    outputs = _clean_list("산출물", values, 10)
    parsed = []
    for output in outputs:
        match = OUTPUT_PATTERN.fullmatch(output)
        if not match:
            raise ValueError(
                '산출물은 각각 "산출물 이름: https://공유링크" 형식으로 작성해주세요.'
            )
        parsed.append((match.group(1).strip(), match.group(2)))
    return parsed


def _clean_references(values: list[str] | None) -> list[tuple[str, str]]:
    """`자료 이름: https://링크` 입력을 중복 없는 참고 자료로 검증합니다."""
    references = _clean_list("참고 자료", values, MAX_REFERENCE_COUNT)
    parsed = []
    seen_urls = set()
    for reference in references:
        match = OUTPUT_PATTERN.fullmatch(reference)
        if not match:
            raise ValueError(
                '참고 자료는 각각 "자료 이름: https://공유링크" 형식으로 작성해주세요.'
            )
        name, url = match.group(1).strip(), match.group(2)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        parsed.append((name, url))
    return parsed


def _validate_publishable(parts: list[str], max_chars: int = 6_000) -> None:
    text = "\n".join(parts)
    if len(text) > max_chars:
        raise ValueError(f"Slack 기록은 전체 {max_chars:,}자 이내로 요약해주세요.")
    if SECRET_PATTERN.search(text):
        raise ValueError("Slack 결과에 토큰이나 비밀값으로 보이는 문자열이 있습니다.")
    if LOCAL_PATH_PATTERN.search(text):
        raise ValueError("Slack 결과에는 로컬 절대경로를 넣지 말고 공유 링크를 쓰세요.")


def _reference_section(references: list[tuple[str, str]]) -> str:
    return "\n".join(
        [
            f"참고 자료 ({len(references)}건):",
            *[f"• {_slack_link(url, name)}" for name, url in references],
        ]
    )


def _section_blocks(text: str, *, expand: bool) -> list[dict[str, Any]]:
    """Slack section 한도를 넘지 않게 줄바꿈 경계로 나눕니다."""
    blocks = []
    remaining = text
    while remaining:
        if len(remaining) <= SLACK_SECTION_TEXT_LIMIT:
            chunk, remaining = remaining, ""
        else:
            cut = remaining.rfind("\n", 0, SLACK_SECTION_TEXT_LIMIT + 1)
            if cut <= 0:
                cut = SLACK_SECTION_TEXT_LIMIT
            chunk = remaining[:cut]
            remaining = remaining[cut:].lstrip("\n")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk},
                "expand": expand,
            }
        )
    return blocks


def _message_blocks(body: str, details: str) -> list[dict[str, Any]]:
    blocks = _section_blocks(body, expand=True)
    if details:
        blocks.append({"type": "divider"})
        blocks.extend(_section_blocks(details, expand=False))
    return blocks


def _format_elapsed(seconds: int) -> str:
    """초 단위 경과시간을 사람에게 필요한 정밀도로 줄입니다."""
    seconds = max(0, seconds)
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    if minutes:
        parts.append(f"{minutes}분")
    if not parts:
        parts.append(f"{seconds}초")
    return " ".join(parts)


def _clean_runtime_label(
    name: str, value: str | None, maximum: int = 100
) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > maximum:
        raise ValueError(f"{name}은 {maximum}자 이내로 작성해주세요.")
    return cleaned


def _clean_usage_value(name: str, value: int | None) -> int | None:
    if value is not None and value < 0:
        raise ValueError(f"{name}은 0 이상의 정수여야 합니다.")
    return value


def _token_usage_text(
    input_tokens: int | None,
    cached_input_tokens: int | None,
    output_tokens: int | None,
    reasoning_output_tokens: int | None,
    total_tokens: int | None,
) -> str:
    values = {
        "입력": input_tokens,
        "캐시 입력": cached_input_tokens,
        "출력": output_tokens,
        "추론 출력": reasoning_output_tokens,
    }
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    parts = [
        f"{label} {value:,}" for label, value in values.items() if value is not None
    ]
    if total_tokens is not None:
        total = f"총 {total_tokens:,}"
        return f"{total} ({' / '.join(parts)})" if parts else total
    return " / ".join(parts) if parts else "수집되지 않음"


def _execution_metadata(
    started_at: str,
    finished_at: float,
    client_name: str | None,
    model: str | None,
    reasoning_effort: str | None,
    input_tokens: int | None,
    cached_input_tokens: int | None,
    output_tokens: int | None,
    reasoning_output_tokens: int | None,
    total_tokens: int | None,
    conversation_turns: int | None,
) -> str:
    try:
        elapsed = round(finished_at - float(started_at))
    except ValueError:
        elapsed = 0
    token_usage = _token_usage_text(
        input_tokens,
        cached_input_tokens,
        output_tokens,
        reasoning_output_tokens,
        total_tokens,
    )
    turns = (
        f"{conversation_turns:,}" if conversation_turns is not None else "수집되지 않음"
    )
    return "\n".join(
        [
            "실행 메타 정보:",
            f"• 도구: {_escape(client_name or '수집되지 않음')}",
            f"• 모델: {_escape(model or '수집되지 않음')}",
            f"• Effort: {_escape(reasoning_effort or '수집되지 않음')}",
            f"• 토큰: {token_usage}",
            f"• 전체 시간: {_format_elapsed(elapsed)}",
            f"• 대화 턴: {turns}",
        ]
    )


def _detail_section(execution_metadata: str, references: list[tuple[str, str]]) -> str:
    sections = ["상세 기록", "", execution_metadata]
    if references:
        sections.extend(["", _reference_section(references)])
    return "\n".join(sections)


def _reference_message(reason: str, references: list[tuple[str, str]]) -> str:
    return f"[참고 자료] {_escape(reason)}\n\n{_reference_section(references)}"


async def record_task_references(
    client: AsyncWebClient,
    list_url: str,
    reason: str,
    references: list[str],
) -> str:
    """조사 게이트에서 실제 판단에 사용한 자료를 작업 스레드에 남깁니다."""
    reason = reason.strip()
    if len(reason) < 2 or len(reason) > 200:
        raise ValueError("조사 이유는 2자 이상 200자 이내로 작성해주세요.")
    curated_references = _clean_references(references)
    if not curated_references:
        raise ValueError("실제로 참고한 자료를 하나 이상 남겨주세요.")
    _validate_publishable(
        [reason, *[f"{name}: {url}" for name, url in curated_references]],
        max_chars=20_000,
    )

    reference = parse_slack_list_task_url(list_url)
    task_schema, record = await _read_record(client, reference)
    work_references = task_schema.work_thread_references_of(record)
    if len(work_references) != 1:
        raise ValueError("먼저 start-slack-list-task로 작업 스레드를 연결해주세요.")

    location = message_location(work_references[0])
    text = _reference_message(reason, curated_references)
    posted = await client.chat_postMessage(
        channel=location.channel_id,
        thread_ts=location.root_ts,
        text=text,
        blocks=_section_blocks(text, expand=False),
    )
    reply_location = SlackMessageLocation(
        channel_id=str(posted.get("channel", location.channel_id)),
        ts=str(posted["ts"]),
        root_ts=location.root_ts,
    )
    permalink = await _permalink(client, reply_location)
    return json.dumps(
        {
            "posted": True,
            "permalink": permalink,
            "reference_count": len(curated_references),
        },
        ensure_ascii=False,
    )


def _result_message(
    title: str,
    status: TaskResultStatus,
    summary: str,
    learnings: list[str],
    reusable_findings: list[str],
    outputs: list[tuple[str, str]],
    validation: list[str],
    remaining: list[str],
) -> str:
    lines = [
        f"[작업 결과] {_escape(title)}",
        "",
        f"결과: {_escape(summary)}",
    ]
    if status != "completed":
        lines.insert(2, f"상태: {STATUS_LABELS[status]}")

    if len(outputs) == 1:
        name, url = outputs[0]
        lines.extend(["", f"산출물: {_slack_link(url, name)}"])
    elif outputs:
        lines.extend(
            ["", "산출물:", *[f"• {_slack_link(url, name)}" for name, url in outputs]]
        )

    sections = (
        ("시행착오·경험", learnings),
        ("재사용할 정보", reusable_findings),
        ("검증", validation),
        ("남은 일", remaining),
    )
    for label, values in sections:
        if values:
            lines.extend(
                ["", f"{label}:", *[f"• {_escape(value)}" for value in values]]
            )
    return "\n".join(lines)


async def publish_task_result(
    client: AsyncWebClient,
    list_url: str,
    actor: str,
    status: TaskResultStatus,
    summary: str,
    outputs: list[str],
    learnings: list[str] | None = None,
    reusable_findings: list[str] | None = None,
    validation: list[str] | None = None,
    remaining: list[str] | None = None,
    references: list[str] | None = None,
    client_name: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    input_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_output_tokens: int | None = None,
    total_tokens: int | None = None,
    conversation_turns: int | None = None,
    mark_completed: bool = True,
) -> str:
    """작업 스레드에 선별한 종료 요약 한 건을 게시합니다."""
    summary = summary.strip()
    if len(summary) < 5 or len(summary) > 1_200:
        raise ValueError("결과 요약은 5자 이상 1,200자 이내로 작성해주세요.")

    curated_learnings = _clean_list("시행착오·경험", learnings, 3)
    curated_findings = _clean_list("재사용할 정보", reusable_findings, 5)
    curated_outputs = _clean_outputs(outputs)
    curated_references = _clean_references(references)
    curated_validation = _clean_list("검증", validation, 5)
    curated_remaining = _clean_list("남은 일", remaining, 5)
    client_name = _clean_runtime_label("도구", client_name)
    model = _clean_runtime_label("모델", model)
    reasoning_effort = _clean_runtime_label("Effort", reasoning_effort, maximum=50)
    input_tokens = _clean_usage_value("입력 토큰", input_tokens)
    cached_input_tokens = _clean_usage_value("캐시 입력 토큰", cached_input_tokens)
    output_tokens = _clean_usage_value("출력 토큰", output_tokens)
    reasoning_output_tokens = _clean_usage_value(
        "추론 출력 토큰", reasoning_output_tokens
    )
    total_tokens = _clean_usage_value("전체 토큰", total_tokens)
    conversation_turns = _clean_usage_value("대화 턴", conversation_turns)
    _validate_publishable(
        [
            summary,
            *curated_learnings,
            *curated_findings,
            *[f"{name}: {url}" for name, url in curated_outputs],
            *[f"{name}: {url}" for name, url in curated_references],
            *curated_validation,
            *curated_remaining,
            *(value for value in (client_name, model, reasoning_effort) if value),
        ],
        max_chars=24_000,
    )

    reference = parse_slack_list_task_url(list_url)
    task_schema, record = await _read_record(client, reference)
    work_references = task_schema.work_thread_references_of(record)
    if len(work_references) != 1:
        raise ValueError("먼저 start-slack-list-task로 작업 스레드를 연결해주세요.")

    location = message_location(work_references[0])
    execution_metadata = _execution_metadata(
        location.root_ts,
        time.time(),
        client_name,
        model,
        reasoning_effort,
        input_tokens,
        cached_input_tokens,
        output_tokens,
        reasoning_output_tokens,
        total_tokens,
        conversation_turns,
    )
    body = _result_message(
        task_schema.title_of(record),
        status,
        summary,
        curated_learnings,
        curated_findings,
        curated_outputs,
        curated_validation,
        curated_remaining,
    )
    details = _detail_section(execution_metadata, curated_references)
    text = f"{body}\n\n{details}"
    blocks = _message_blocks(body, details)
    client_msg_id = result_client_msg_id(reference.list_id, reference.record_id)
    message_ts = await find_message_ts(client, location, client_msg_id)
    if message_ts:
        await client.chat_update(
            channel=location.channel_id,
            ts=message_ts,
            text=text,
            blocks=blocks,
        )
        message_action = "updated"
    else:
        posted = await client.chat_postMessage(
            channel=location.channel_id,
            thread_ts=location.root_ts,
            client_msg_id=client_msg_id,
            text=text,
            blocks=blocks,
        )
        message_ts = str(posted["ts"])
        message_action = "created"

    result_location = SlackMessageLocation(
        channel_id=location.channel_id,
        ts=message_ts,
        root_ts=location.root_ts,
    )
    permalink = await _permalink(client, result_location)

    list_should_be_completed = mark_completed and status == "completed"
    list_marked_completed = False
    list_update_error = None
    if list_should_be_completed:
        try:
            await client.slackLists_items_update(
                list_id=reference.list_id,
                cells=build_completion_cells(
                    task_schema.completed_column_id, [reference.record_id]
                ),
            )
            list_marked_completed = True
        except (SlackApiError, SlackRequestError) as exc:
            list_update_error = (
                str(exc.response.get("error", "slack_api_error"))
                if isinstance(exc, SlackApiError)
                else "slack_request_error"
            )

    completion_reaction_added = False
    if list_marked_completed:
        try:
            await client.reactions_add(
                channel=location.channel_id,
                timestamp=location.root_ts,
                name="white_check_mark",
            )
            completion_reaction_added = True
        except (SlackApiError, SlackRequestError):
            # 결과 댓글과 List 완료는 이미 반영됐으므로, 장식용 반응 실패로 종료를 막지 않는다.
            pass

    return json.dumps(
        {
            "posted": True,
            "status": status,
            "outcome": "partial_success" if list_update_error else "success",
            "message_action": message_action,
            "permalink": permalink,
            "list_marked_completed": list_marked_completed,
            "list_update_error": list_update_error,
            "retry_post": False,
            "completion_reaction_added": completion_reaction_added,
        },
        ensure_ascii=False,
    )
