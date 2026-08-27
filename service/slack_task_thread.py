"""Slack List 작업을 공용 작업 스레드와 연결합니다.

에이전트의 작업 중 대화는 Slack으로 복사하지 않습니다. 이 모듈은 작업 시작 때
결과를 남길 루트를 연결하고, 실제 작업이 끝났을 때 선별한 결과와 경험 한 건을
게시하는 두 동작만 제공합니다.
"""

import asyncio
import html
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import psycopg
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from service.db import get_dsn

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


@dataclass(frozen=True)
class SlackListTaskReference:
    """Slack List URL에서 읽은 작업 식별자입니다."""

    list_url: str
    list_id: str
    record_id: str


@dataclass(frozen=True)
class SlackMessageLocation:
    """Slack 메시지와 그 메시지가 속한 루트 스레드 위치입니다."""

    channel_id: str
    ts: str
    root_ts: str
    permalink: str | None = None


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

    def completion_cells(self, row_ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "row_id": row_id,
                "column_id": self.completed_column_id,
                "checkbox": [True],
            }
            for row_id in row_ids
        ]


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


def message_location(reference: dict[str, Any]) -> SlackMessageLocation:
    """List message 셀의 참조를 Slack API 호출 위치로 바꿉니다."""
    permalink = reference.get("value") or reference.get("permalink")
    channel_id = reference.get("channel_id")
    ts = reference.get("ts")
    thread_ts = reference.get("thread_ts")

    if permalink:
        parsed = urlparse(str(permalink))
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) >= 3 and re.fullmatch(r"[CDG][A-Z0-9]+", segments[-2]):
            channel_id = channel_id or segments[-2]
            path_ts = segments[-1]
            if path_ts.startswith("p") and path_ts[1:].isdigit():
                digits = path_ts[1:]
                if len(digits) > 6:
                    ts = ts or f"{digits[:-6]}.{digits[-6:]}"
        query = parse_qs(parsed.query)
        thread_ts = thread_ts or (query.get("thread_ts") or [None])[0]
        channel_id = channel_id or (query.get("cid") or [None])[0]

    if not channel_id or not ts:
        raise ValueError("List의 Slack 메시지 링크에서 채널과 시각을 읽지 못했습니다.")

    return SlackMessageLocation(
        channel_id=str(channel_id),
        ts=str(ts),
        root_ts=str(thread_ts or ts),
        permalink=str(permalink) if permalink else None,
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
    response = await client.slackLists_items_info(
        list_id=reference.list_id, id=reference.record_id
    )
    schema = task_list_schema(response["list"]["list_metadata"]["schema"])
    return schema, response["record"]


async def _permalink(
    client: AsyncWebClient, location: SlackMessageLocation, root: bool = False
) -> str:
    if location.permalink and not root:
        return location.permalink
    message_ts = location.root_ts if root else location.ts
    response = await client.chat_getPermalink(
        channel=location.channel_id, message_ts=message_ts
    )
    return response["permalink"]


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


async def _source_permalink(
    client: AsyncWebClient, reference: dict[str, Any]
) -> str | None:
    """요청 맥락을 못 읽어도 원래 permalink가 있으면 시작 메시지에 보존합니다."""
    try:
        return await _permalink(client, message_location(reference), root=True)
    except SlackApiError:
        return reference.get("value") or reference.get("permalink")
    except ValueError:
        return None


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


def _start_message(
    reference: SlackListTaskReference,
    title: str,
    source_links: list[str],
    actor: str,
) -> str:
    if source_links:
        source = "\n".join(
            f"• {_slack_link(link, f'요청 맥락 {index + 1}')}"
            for index, link in enumerate(source_links)
        )
    else:
        source = "없음 — List 필드를 시작 맥락으로 사용"

    return (
        f"[시작] {_escape(title)}\n\n"
        f"Slack List: {_slack_link(reference.list_url, '작업 행')}\n"
        f"요청 맥락:\n{source}\n"
        "기록 방식: 작업 종료 시 결과와 선별한 시행착오·경험 한 건\n"
        f"시작한 사람: {_escape(actor)}"
    )


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
                source_link_results = await asyncio.gather(
                    *(_source_permalink(client, item) for item in source_references)
                )
                source_links = [link for link in source_link_results if link]
                posted = await client.chat_postMessage(
                    channel=channel_id,
                    text=_start_message(
                        reference,
                        task_schema.title_of(record),
                        source_links,
                        actor,
                    ),
                )
                root_location = SlackMessageLocation(
                    channel_id=str(posted.get("channel", channel_id)),
                    ts=str(posted["ts"]),
                    root_ts=str(posted["ts"]),
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
        "recording_rule": (
            "작업 중 대화는 에이전트 안에 둡니다. 실제 작업이 완료되거나 막힘·인계로 "
            "종료될 때만 publish_slack_task_result를 한 번 호출합니다."
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


def _validate_publishable(parts: list[str]) -> None:
    text = "\n".join(parts)
    if len(text) > 6_000:
        raise ValueError("Slack 작업 결과는 전체 6,000자 이내로 요약해주세요.")
    if SECRET_PATTERN.search(text):
        raise ValueError("Slack 결과에 토큰이나 비밀값으로 보이는 문자열이 있습니다.")
    if LOCAL_PATH_PATTERN.search(text):
        raise ValueError("Slack 결과에는 로컬 절대경로를 넣지 말고 공유 링크를 쓰세요.")


def _result_message(
    title: str,
    status: TaskResultStatus,
    summary: str,
    learnings: list[str],
    reusable_findings: list[str],
    outputs: list[str],
    validation: list[str],
    remaining: list[str],
    actor: str,
) -> str:
    lines = [
        f"[작업 결과] {_escape(title)}",
        "",
        f"상태: {STATUS_LABELS[status]}",
        f"결과: {_escape(summary)}",
    ]

    sections = (
        ("시행착오·경험", learnings),
        ("재사용할 정보", reusable_findings),
        ("산출물", outputs),
        ("검증", validation),
        ("남은 일", remaining),
    )
    for label, values in sections:
        if values:
            lines.extend(
                ["", f"{label}:", *[f"• {_escape(value)}" for value in values]]
            )
    lines.append("")
    lines.append(f"게시한 사람: {_escape(actor)}")
    return "\n".join(lines)


async def publish_task_result(
    client: AsyncWebClient,
    list_url: str,
    actor: str,
    status: TaskResultStatus,
    summary: str,
    learnings: list[str] | None = None,
    reusable_findings: list[str] | None = None,
    outputs: list[str] | None = None,
    validation: list[str] | None = None,
    remaining: list[str] | None = None,
    mark_completed: bool = False,
) -> str:
    """작업 스레드에 선별한 종료 요약 한 건을 게시합니다."""
    summary = summary.strip()
    if len(summary) < 5 or len(summary) > 1_200:
        raise ValueError("결과 요약은 5자 이상 1,200자 이내로 작성해주세요.")
    if mark_completed and status != "completed":
        raise ValueError("완료 상태의 결과만 Slack List를 완료 처리할 수 있습니다.")

    curated_learnings = _clean_list("시행착오·경험", learnings, 3)
    curated_findings = _clean_list("재사용할 정보", reusable_findings, 5)
    curated_outputs = _clean_list("산출물", outputs, 10)
    curated_validation = _clean_list("검증", validation, 5)
    curated_remaining = _clean_list("남은 일", remaining, 5)
    _validate_publishable(
        [
            summary,
            *curated_learnings,
            *curated_findings,
            *curated_outputs,
            *curated_validation,
            *curated_remaining,
        ]
    )

    reference = parse_slack_list_task_url(list_url)
    task_schema, record = await _read_record(client, reference)
    work_references = task_schema.work_thread_references_of(record)
    if len(work_references) != 1:
        raise ValueError("먼저 start_slack_list_task로 작업 스레드를 연결해주세요.")

    location = message_location(work_references[0])
    posted = await client.chat_postMessage(
        channel=location.channel_id,
        thread_ts=location.root_ts,
        text=_result_message(
            task_schema.title_of(record),
            status,
            summary,
            curated_learnings,
            curated_findings,
            curated_outputs,
            curated_validation,
            curated_remaining,
            actor,
        ),
    )

    if mark_completed:
        await client.slackLists_items_update(
            list_id=reference.list_id,
            cells=task_schema.completion_cells([reference.record_id]),
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
            "status": status,
            "permalink": permalink,
            "list_marked_completed": mark_completed,
        },
        ensure_ascii=False,
    )
