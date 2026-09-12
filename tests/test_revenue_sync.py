"""일간 배치의 외부 조회와 dry-run/알림 동작."""

from unittest.mock import AsyncMock, Mock

import pytest
from revenue_fixture import SOURCES, TAXONOMY, make_raw

from scripts import sync_revenue_ledger as batch
from service.revenue.enrich import crm_list


@pytest.fixture
def source_data(monkeypatch):
    monkeypatch.setattr(batch, "load_sources", lambda: SOURCES)
    monkeypatch.setattr(
        batch,
        "load_rules",
        lambda: {
            "product": TAXONOMY["views"]["상품"],
            "assertions": TAXONOMY["assertions"],
        },
    )
    monkeypatch.setattr(batch, "fetch_ledger", lambda sources: make_raw())
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


def test_dry_run_does_not_connect_database_or_send_slack(source_data, monkeypatch):
    db = Mock(side_effect=AssertionError("dry-run must not connect"))
    slack = Mock(side_effect=AssertionError("dry-run must not send"))
    monkeypatch.setattr(batch, "connect", db)
    monkeypatch.setattr(batch, "WebClient", slack)
    batch.main(dry_run=True)
    db.assert_not_called()
    slack.assert_not_called()


def test_crm_list_preserves_embedded_comma_and_all_values():
    assert crm_list('["A,B", "C", null, ""]') == ["A,B", "C"]
    assert crm_list(None) == []
