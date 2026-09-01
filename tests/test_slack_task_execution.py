"""작업 실행 사용량 DB 적재 테스트."""

from unittest.mock import MagicMock, patch

import pytest

from service.slack_task_execution import (
    TaskExecutionMetrics,
    fallback_execution_id,
    record_task_execution,
)


def test_fallback_execution_id_is_stable_per_task_and_service():
    first = fallback_execution_id("https://slack.example/task", "Codex")
    second = fallback_execution_id("https://slack.example/task", "Codex")

    assert first == second
    assert first != fallback_execution_id("https://slack.example/task", "Claude Code")


def test_metrics_reject_negative_usage():
    with pytest.raises(ValueError, match="0 이상의 정수"):
        TaskExecutionMetrics(total_tokens=-1)


def test_complete_metrics_require_collected_usage():
    with pytest.raises(ValueError, match="전체 토큰"):
        TaskExecutionMetrics(collection_status="complete")


def test_record_upserts_one_execution():
    cursor = MagicMock()
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connect_context = MagicMock()
    connect_context.__enter__.return_value = connection
    metrics = TaskExecutionMetrics(
        service="Codex",
        execution_id="run-1",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        total_tokens=168,
        collector_version="tmn-operating/0.1.3",
        collection_status="complete",
    )

    with patch("service.slack_task_execution.connect", return_value=connect_context):
        execution_id = record_task_execution(
            list_url="https://slack.example/task",
            status="completed",
            task_started_ts="1700000000.000000",
            task_finished_ts=1700003600.0,
            metrics=metrics,
        )

    assert execution_id == "run-1"
    cursor.execute.assert_called_once()
    sql, values = cursor.execute.call_args.args
    assert "ON CONFLICT (list_url, execution_id)" in sql
    assert values["total_tokens"] == 168
    assert (values["task_finished_at"] - values["task_started_at"]).seconds == 3600
