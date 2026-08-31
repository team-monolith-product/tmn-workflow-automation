"""작업 실행 사용량 DB 적재 테스트."""

from unittest.mock import MagicMock, patch

import pytest
from psycopg.types.json import Jsonb

from service.slack_task_execution import (
    TaskExecutionMetrics,
    fallback_execution_id,
    record_task_execution,
)


def test_fallback_execution_id_is_stable_per_task_and_service():
    first = fallback_execution_id("F01", "Rec01", "Codex")
    second = fallback_execution_id("F01", "Rec01", "Codex")

    assert first == second
    assert first != fallback_execution_id("F01", "Rec01", "Claude Code")


def test_metrics_reject_negative_usage():
    with pytest.raises(ValueError, match="0 이상의 정수"):
        TaskExecutionMetrics(total_tokens=-1)


def test_record_upserts_one_execution_with_model_details():
    cursor = MagicMock()
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connect_context = MagicMock()
    connect_context.__enter__.return_value = connection
    connect_context.__exit__.return_value = False
    usage = [
        {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "is_subagent": False,
            "agent_count": 1,
            "total_tokens": 120,
        },
        {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
            "is_subagent": True,
            "agent_count": 3,
            "total_tokens": 48,
        },
    ]
    metrics = TaskExecutionMetrics(
        service="Codex",
        execution_id="run-1",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        input_tokens=140,
        cached_input_tokens=90,
        output_tokens=28,
        reasoning_output_tokens=7,
        total_tokens=168,
        conversation_turns=2,
        usage_by_model=usage,
        collector_version="tmn-operating/0.1.3",
        collection_status="complete",
    )

    with patch("service.slack_task_execution.connect", return_value=connect_context):
        execution_id = record_task_execution(
            list_id="F01",
            record_id="Rec01",
            list_url="https://example.slack.com/lists/T/F01?record_id=Rec01",
            actor="owner@example.com",
            status="completed",
            task_started_ts="1700000000.000000",
            task_finished_ts=1700003600.0,
            metrics=metrics,
        )

    assert execution_id == "run-1"
    cursor.execute.assert_called_once()
    sql, values = cursor.execute.call_args.args
    assert "ON CONFLICT (list_id, record_id, execution_id)" in sql
    assert values["model"] == "gpt-5.6-sol"
    assert (values["task_finished_at"] - values["task_started_at"]).seconds == 3600
    assert isinstance(values["usage_by_model"], Jsonb)
