from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from revenue_fixture import TABS, sheet_response

from scripts import sync_revenue_ledger as batch
from scripts.sync_revenue_ledger import KST
from service.revenue.enrich import crm_list


@pytest.fixture
def source_data(monkeypatch):
    monkeypatch.setattr("service.revenue.normalize.TABS", TABS)
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


COMPLETED_AT = datetime(2026, 9, 14, 0, 10, tzinfo=KST)


def totals(**years):
    return {
        year: {
            "issued": Decimal(issued),
            "issued_count": issued_count,
            "planned": Decimal(planned),
            "planned_count": planned_count,
        }
        for year, (issued, issued_count, planned, planned_count) in years.items()
    }


@pytest.fixture
def synced(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr(batch, "connect", connection)
    monkeypatch.setattr(batch, "year_totals", lambda conn: {})
    notify = Mock()
    monkeypatch.setattr(batch, "notify", notify)
    return connection, notify


@pytest.mark.parametrize("dry_run", [False, True])
def test_normalization_errors_are_reported_without_blocking_sync(
    monkeypatch, synced, dry_run
):
    connection, notify = synced
    rows = [{"customer": "학교"}]
    warnings = ["미분류", "CRM 매칭 0건"]
    monkeypatch.setattr(batch, "collect_rows", lambda: (rows, warnings))
    replace = Mock()
    monkeypatch.setattr(batch, "replace_rows", replace)
    report = Mock()
    monkeypatch.setattr(batch.sentry_sdk, "capture_message", report)
    batch.main(dry_run=dry_run)
    if dry_run:
        connection.assert_not_called()
        replace.assert_not_called()
        report.assert_not_called()
        notify.assert_not_called()
    else:
        replace.assert_called_once_with(
            connection.return_value.__enter__.return_value, rows
        )
        report.assert_called_once_with(
            "매출장 정규화 오류\n미분류\nCRM 매칭 0건", level="error"
        )
        text = notify.call_args.args[0]
        assert ":warning: 정규화 경고 2건" in text
        assert "• 미분류" in text and "• CRM 매칭 0건" in text


def test_failure_notifies_slack_and_still_raises(monkeypatch, synced):
    _, notify = synced
    monkeypatch.setattr(
        batch, "collect_rows", Mock(side_effect=RuntimeError("시트 400"))
    )
    with pytest.raises(RuntimeError, match="시트 400"):
        batch.main()
    assert notify.call_args.args[0] == (
        ":x: 매출장 동기화 실패 — 대시보드는 이전 데이터 그대로입니다.\nRuntimeError: 시트 400"
    )


def test_notify_failure_does_not_hide_the_original_error(monkeypatch, synced):
    _, notify = synced
    notify.side_effect = RuntimeError("slack down")
    monkeypatch.setattr(
        batch, "collect_rows", Mock(side_effect=ValueError("빈 매출장"))
    )
    with pytest.raises(RuntimeError, match="slack down") as excinfo:
        batch.main()
    assert isinstance(excinfo.value.__context__, ValueError)


def test_report_shows_day_over_day_change_per_year():
    before = totals(
        **{
            "2026": (2_500_000_000, 130, 1_400_000_000, 19),
            "2025": (5_610_314_600, 185, 0, 0),
        }
    )
    after = totals(
        **{
            "2026": (2_638_524_903, 134, 1_400_101_557, 19),
            "2025": (5_610_314_600, 185, 0, 0),
        }
    )
    text = batch.format_report(before, after, COMPLETED_AT, [])
    assert text.splitlines() == [
        "매출장 반영 09-14 00:10 · 발행 319건 · 예정 19건",
        "2026 발행 26.39억 (+138,524,903원 · +4건) · 예정 14.00억 (+101,557원 · +0건)",
    ]


def test_report_keeps_latest_year_even_without_change_and_lists_new_years():
    before = totals(**{"2026": (100, 1, 0, 0)})
    after = totals(**{"2027": (50, 1, 0, 0), "2026": (100, 1, 0, 0)})
    text = batch.format_report(before, after, COMPLETED_AT, [])
    assert text.splitlines()[1:] == [
        "2027 발행 0.00억 (+50원 · +1건) · 예정 0.00억 (변동 없음)",
    ]
    unchanged = batch.format_report(after, after, COMPLETED_AT, [])
    assert unchanged.splitlines()[1:] == [
        "2027 발행 0.00억 (변동 없음) · 예정 0.00억 (변동 없음)",
    ]


def test_report_truncates_warnings_to_five():
    text = batch.format_report({}, {}, COMPLETED_AT, [f"경고 {i}" for i in range(7)])
    assert ":warning: 정규화 경고 7건" in text
    assert "• 경고 4" in text and "• 경고 5" not in text
