"""Slack List 작업의 에이전트 실행 사용량을 Postgres에 기록합니다."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from service.db import connect

UPSERT_USAGE = """
INSERT INTO task_execution_usage (
    list_url, execution_id, status, service, model, reasoning_effort,
    total_tokens, task_started_at, task_finished_at,
    collector_version, collection_status
) VALUES (
    %(list_url)s, %(execution_id)s, %(status)s, %(service)s, %(model)s,
    %(reasoning_effort)s, %(total_tokens)s, %(task_started_at)s,
    %(task_finished_at)s, %(collector_version)s, %(collection_status)s
)
ON CONFLICT (list_url, execution_id) DO UPDATE SET
    status = EXCLUDED.status,
    service = EXCLUDED.service,
    model = EXCLUDED.model,
    reasoning_effort = EXCLUDED.reasoning_effort,
    total_tokens = EXCLUDED.total_tokens,
    task_started_at = EXCLUDED.task_started_at,
    task_finished_at = EXCLUDED.task_finished_at,
    collector_version = EXCLUDED.collector_version,
    collection_status = EXCLUDED.collection_status
"""


@dataclass(frozen=True)
class TaskExecutionMetrics:
    """에이전트 훅이 한 작업 실행에서 수집한 최소 분석 값입니다."""

    service: str = "MCP 클라이언트"
    execution_id: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    total_tokens: int | None = None
    collector_version: str | None = None
    collection_status: Literal["complete", "partial", "unavailable"] = "unavailable"

    def __post_init__(self) -> None:
        if self.total_tokens is not None and self.total_tokens < 0:
            raise ValueError("전체 토큰은 0 이상의 정수여야 합니다.")
        if self.collection_status == "complete" and self.total_tokens is None:
            raise ValueError("수집 완료 상태에는 전체 토큰이 필요합니다.")


def fallback_execution_id(list_url: str, service: str) -> str:
    """훅이 없는 구형 클라이언트의 재호출도 한 실행으로 접습니다."""
    value = f"legacy\0{list_url}\0{service}".encode()
    return f"legacy-{hashlib.sha256(value).hexdigest()}"


def record_task_execution(
    *,
    list_url: str,
    status: str,
    task_started_ts: str,
    task_finished_ts: float,
    metrics: TaskExecutionMetrics,
) -> str:
    """같은 실행의 사용량을 한 행으로 UPSERT합니다."""
    execution_id = metrics.execution_id or fallback_execution_id(
        list_url, metrics.service
    )
    values = {
        "list_url": list_url,
        "execution_id": execution_id,
        "status": status,
        "service": metrics.service,
        "model": metrics.model,
        "reasoning_effort": metrics.reasoning_effort,
        "total_tokens": metrics.total_tokens,
        "task_started_at": datetime.fromtimestamp(float(task_started_ts), timezone.utc),
        "task_finished_at": datetime.fromtimestamp(task_finished_ts, timezone.utc),
        "collector_version": metrics.collector_version,
        "collection_status": metrics.collection_status,
    }

    with connect() as conn, conn.cursor() as cur:
        cur.execute(UPSERT_USAGE, values)

    return execution_id
