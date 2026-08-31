"""Slack List 작업의 에이전트 실행 사용량을 Postgres에 기록합니다."""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from psycopg.types.json import Jsonb

from service.db import connect

UPSERT_USAGE = """
INSERT INTO task_execution_usage (
    list_id, record_id, list_url, execution_id, actor, status, service,
    model, reasoning_effort,
    input_tokens, cached_input_tokens, cache_write_input_tokens,
    output_tokens, reasoning_output_tokens, total_tokens, conversation_turns,
    usage_by_model,
    task_started_at, task_finished_at,
    collector_version, collection_status
) VALUES (
    %(list_id)s, %(record_id)s, %(list_url)s, %(execution_id)s, %(actor)s,
    %(status)s, %(service)s, %(model)s, %(reasoning_effort)s,
    %(input_tokens)s, %(cached_input_tokens)s,
    %(cache_write_input_tokens)s, %(output_tokens)s,
    %(reasoning_output_tokens)s, %(total_tokens)s, %(conversation_turns)s,
    %(usage_by_model)s,
    %(task_started_at)s, %(task_finished_at)s,
    %(collector_version)s, %(collection_status)s
)
ON CONFLICT (list_id, record_id, execution_id) DO UPDATE SET
    list_url = EXCLUDED.list_url,
    actor = EXCLUDED.actor,
    status = EXCLUDED.status,
    service = EXCLUDED.service,
    model = EXCLUDED.model,
    reasoning_effort = EXCLUDED.reasoning_effort,
    input_tokens = EXCLUDED.input_tokens,
    cached_input_tokens = EXCLUDED.cached_input_tokens,
    cache_write_input_tokens = EXCLUDED.cache_write_input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    reasoning_output_tokens = EXCLUDED.reasoning_output_tokens,
    total_tokens = EXCLUDED.total_tokens,
    conversation_turns = EXCLUDED.conversation_turns,
    usage_by_model = EXCLUDED.usage_by_model,
    task_started_at = EXCLUDED.task_started_at,
    task_finished_at = EXCLUDED.task_finished_at,
    collector_version = EXCLUDED.collector_version,
    collection_status = EXCLUDED.collection_status,
    recorded_at = now()
"""


@dataclass(frozen=True)
class TaskExecutionMetrics:
    """에이전트 훅이 한 작업 실행에서 수집한 분석 값입니다."""

    service: str = "MCP 클라이언트"
    execution_id: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    conversation_turns: int | None = None
    usage_by_model: list[dict[str, Any]] = field(default_factory=list)
    collector_version: str | None = None
    collection_status: Literal["complete", "partial", "unavailable"] = "unavailable"

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.cache_write_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
            self.total_tokens,
            self.conversation_turns,
        )
        if any(value is not None and value < 0 for value in values):
            raise ValueError("실행 사용량은 0 이상의 정수여야 합니다.")


def fallback_execution_id(list_id: str, record_id: str, service: str) -> str:
    """훅이 없는 구형 클라이언트의 재호출도 한 실행으로 접습니다."""
    value = f"legacy\0{list_id}\0{record_id}\0{service}".encode()
    return f"legacy-{hashlib.sha256(value).hexdigest()}"


def record_task_execution(
    *,
    list_id: str,
    record_id: str,
    list_url: str,
    actor: str,
    status: str,
    task_started_ts: str,
    task_finished_ts: float,
    metrics: TaskExecutionMetrics,
) -> str:
    """같은 실행의 사용량을 한 행으로 UPSERT합니다."""
    execution_id = metrics.execution_id or fallback_execution_id(
        list_id, record_id, metrics.service
    )
    started_at = datetime.fromtimestamp(float(task_started_ts), timezone.utc)
    finished_at = datetime.fromtimestamp(task_finished_ts, timezone.utc)
    values = {
        "list_id": list_id,
        "record_id": record_id,
        "list_url": list_url,
        "execution_id": execution_id,
        "actor": actor,
        "status": status,
        "service": metrics.service,
        "model": metrics.model,
        "reasoning_effort": metrics.reasoning_effort,
        "input_tokens": metrics.input_tokens,
        "cached_input_tokens": metrics.cached_input_tokens,
        "cache_write_input_tokens": metrics.cache_write_input_tokens,
        "output_tokens": metrics.output_tokens,
        "reasoning_output_tokens": metrics.reasoning_output_tokens,
        "total_tokens": metrics.total_tokens,
        "conversation_turns": metrics.conversation_turns,
        "usage_by_model": Jsonb(metrics.usage_by_model),
        "task_started_at": started_at,
        "task_finished_at": finished_at,
        "collector_version": metrics.collector_version,
        "collection_status": metrics.collection_status,
    }

    with connect() as conn, conn.cursor() as cur:
        cur.execute(UPSERT_USAGE, values)

    return execution_id
