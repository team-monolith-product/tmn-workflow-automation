from unittest.mock import AsyncMock, Mock

import pytest
from revenue_fixture import RULES, SOURCES, sheet_response

from scripts import sync_revenue_ledger as batch
from service.revenue.enrich import crm_list


@pytest.fixture
def source_data(monkeypatch):
    monkeypatch.setattr(batch, "load_sources", lambda: SOURCES)
    monkeypatch.setattr(batch, "load_rules", lambda: RULES)
    monkeypatch.setattr(
        "service.revenue.normalize.get_spreadsheet_values_batch",
        lambda *args, **kwargs: sheet_response(),
    )
    crm = AsyncMock(
        return_value=(
            [{"school": "도담고등학교", "level": "고등학교", "office": "세종교육청"}],
            [
                {
                    "school": "도담고등학교",
                    "budget": '["자체,정책", "교육청", null]',
                    "terms": '["1학기"]',
                }
            ],
            [],
        )
    )
    monkeypatch.setattr(batch, "fetch_crm", crm)
    return crm


def test_collection_includes_live_crm_in_same_batch(source_data):
    rows, warnings = batch.collect_rows()
    assert len(rows) == 5 and warnings == []
    school = next(r for r in rows if r["customer"] == "도담고등학교")
    assert school["edu_office"] == "세종교육청"
    assert school["budget_sources"] == ["자체,정책", "교육청"]
    source_data.assert_awaited_once()


def test_dry_run_does_not_connect_database_or_report_sentry(source_data, monkeypatch):
    db = Mock(side_effect=AssertionError("dry-run must not connect"))
    report = Mock(side_effect=AssertionError("dry-run must not send"))
    monkeypatch.setattr(batch, "connect", db)
    monkeypatch.setattr(batch.sentry_sdk, "capture_message", report)
    batch.main(dry_run=True)
    db.assert_not_called()
    report.assert_not_called()


def test_crm_list_preserves_embedded_comma_and_all_values():
    assert crm_list('["A,B", "C", null, ""]') == ["A,B", "C"]
    assert crm_list(None) == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_normalization_errors_are_reported_without_blocking_sync(monkeypatch, dry_run):
    from unittest.mock import MagicMock

    rows = [{"customer": "학교"}]
    warnings = ["미분류", "CRM 매칭 0건"]
    monkeypatch.setattr(batch, "collect_rows", lambda: (rows, warnings))
    connection = MagicMock()
    monkeypatch.setattr(batch, "connect", connection)
    replace = Mock()
    monkeypatch.setattr(batch, "replace_rows", replace)
    report = Mock()
    monkeypatch.setattr(batch.sentry_sdk, "capture_message", report)
    batch.main(dry_run=dry_run)
    if dry_run:
        connection.assert_not_called()
        replace.assert_not_called()
        report.assert_not_called()
    else:
        replace.assert_called_once_with(
            connection.return_value.__enter__.return_value, rows
        )
        report.assert_called_once_with(
            "매출장 정규화 오류\n미분류\nCRM 매칭 0건", level="error"
        )
