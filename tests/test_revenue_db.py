import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from revenue_fixture import normalized_rows

from scripts import sync_revenue_ledger as batch
from service.db import connect, fetch_all
from service.knowledge.query import run_query
from service.revenue.query import dashboard_data


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("REVENUE_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("REVENUE_TEST_DATABASE_URL이 필요합니다")
    schema = "revenue_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        test_dsn = make_conninfo(dsn, options=f"-c search_path={schema}")
        monkeypatch.setenv("KNOWLEDGE_DATABASE_URL", test_dsn)
        try:
            with connect() as conn:
                conn.execute(
                    (
                        Path(__file__).resolve().parents[1]
                        / "migrations/knowledge/006_revenue_transactions.sql"
                    ).read_text()
                )
                conn.execute(
                    "CREATE TABLE query_log (actor text, tool text, query text, filters jsonb, latency_ms int)"
                )
                rows, _ = normalized_rows()
                batch.replace_rows(conn, rows)
            yield test_dsn
        finally:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def transactions():
    with connect(read_only=True) as conn:
        return fetch_all(
            conn, "SELECT * FROM revenue_transactions ORDER BY year, customer, status"
        )


def test_replace_is_idempotent_and_reflects_deletions(database):
    rows, _ = normalized_rows()
    with connect() as conn:
        batch.replace_rows(conn, rows)
    assert len(transactions()) == 5
    replacement = [dict(rows[0], amount=Decimal("0.123456")), rows[0]]
    with connect() as conn:
        batch.replace_rows(conn, replacement)
    assert len(transactions()) == 2
    assert {r["amount"] for r in transactions()} == {
        Decimal("0.123456"),
        Decimal(1000000),
    }


def test_failed_insert_rolls_back_delete_and_retains_previous_snapshot(database):
    before = transactions()
    rows, _ = normalized_rows()
    rows[-1]["status"] = "invalid"
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as conn:
            batch.replace_rows(conn, rows)
    assert transactions() == before


def test_readers_keep_old_snapshot_until_commit(database):
    rows, _ = normalized_rows()
    with connect() as conn:
        batch.replace_rows(conn, rows[:1])
        assert len(transactions()) == 5
    assert len(transactions()) == 1


def test_batch_crm_failure_leaves_database_unchanged(database, monkeypatch):
    before = transactions()

    def failed_collection():
        raise RuntimeError("CRM unavailable")

    monkeypatch.setattr(batch, "collect_rows", failed_collection)
    with pytest.raises(RuntimeError, match="CRM unavailable"):
        batch.main()
    assert transactions() == before


def test_batch_success_replaces_rows_and_dry_run_does_not(database, monkeypatch):
    rows, _ = normalized_rows()
    monkeypatch.setattr(batch, "collect_rows", lambda: (rows[:1], []))
    batch.main(dry_run=True)
    assert len(transactions()) == 5
    batch.main()
    assert len(transactions()) == 1


def test_dashboard_and_query_knowledge_share_amount_and_year_basis(database):
    data = dashboard_data(2025)
    assert data["totals"]["issued"] == 2000000
    assert data["monthly"] == [
        {"month": 3, "amount": 1000000},
        {"month": 12, "amount": 1000000},
    ]
    assert data["totals"]["planned"] == 0
    assert dashboard_data(2026)["totals"]["planned"] == 300000
    text = run_query(
        "SELECT sum(amount) AS amount FROM revenue_transactions WHERE year=2025 AND status='issued'",
        "test@example.com",
        "mcp",
    )
    assert "2000000" in text
    with connect(read_only=True) as conn:
        assert fetch_all(conn, "SELECT actor, tool FROM query_log") == [
            {"actor": "test@example.com", "tool": "mcp"}
        ]
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM revenue_transactions")


def test_crm_arrays_and_program_amounts_are_preserved_without_multiplying_total(
    database,
):
    rows, _ = normalized_rows()
    rows[2].update(
        budget_sources=["자체", "정책", "자체"],
        terms=["1학기", "2학기"],
        program_budget=Decimal("10000000.25"),
    )
    with connect() as conn:
        batch.replace_rows(conn, rows)
    data = dashboard_data(2026, "budget_sources")
    assert data["totals"]["issued"] == 1500000
    assert {r["label"]: r["amount"] for r in data["groups"]} == {
        "자체": 1000000,
        "정책": 1000000,
        "미기재": 500000,
    }
    assert data["rows"][0]["program_budget"] == Decimal("10000000.25")


def test_filters_paging_and_query_injection(database):
    rows, _ = normalized_rows()
    with connect() as conn:
        batch.replace_rows(
            conn,
            [dict(rows[0], item=f"item-{i:03d}", amount=i) for i in range(105)]
            + [rows[-1]],
        )
    page = dashboard_data(2025, page=2)
    assert page["count"] == 105 and len(page["rows"]) == 5
    assert {r["item"] for r in page["rows"]} == {f"item-{i:03d}" for i in range(5)}
    assert dashboard_data(2026, status="planned")["count"] == 1
    assert dashboard_data(2025, search="' OR true --")["count"] == 0
    with pytest.raises(ValueError):
        dashboard_data(dimension="category); DROP TABLE revenue_transactions;--")
    assert len(transactions()) == 106


def test_empty_database_renders_zero_totals(database):
    with connect() as conn:
        conn.execute("DELETE FROM revenue_transactions")
    data = dashboard_data()
    assert data["year"] is None
    assert data["rows"] == [] and data["totals"]["issued"] == 0


def test_charts_cover_all_rows_and_years_independently_of_detail_page(database):
    rows, _ = normalized_rows()
    issued = [
        dict(
            rows[2],
            customer=f"customer-{i:03d}",
            amount=Decimal(i + 1),
            category="구독",
            subcategory="연간",
            budget_sources=["자체", "자체", "정책"],
        )
        for i in range(105)
    ]
    with connect() as conn:
        batch.replace_rows(conn, issued + rows[:2] + [rows[-1]])
    data = dashboard_data(2026, page=2, search="customer-000")
    assert len(data["rows"]) == 1
    assert data["totals"]["count"] == 105
    assert len(data["charts"]["customers"]) == 105
    assert sum(r["amount"] for r in data["charts"]["customers"]) == 5565
    assert {r["year"] for r in data["charts"]["monthly"]} == {2025, 2026}
    group = next(r for r in data["charts"]["groups"] if r["year"] == 2026)
    assert group["amount"] == 5565 and group["count"] == 105 and group["matched"] == 1
    assert data["charts"]["subgroups"][0]["amount"] == 1
    assert sum(r["count"] for r in data["charts"]["sizes"]) == 105
    assert data["charts"]["size_stats"]["median"] == 53
    assert data["totals"]["planned_count"] == 1
    budget = dashboard_data(2026, "budget_sources", bucket="自체")
    assert budget["count"] == 0
    budget = dashboard_data(2026, "budget_sources", bucket="자체", page=2)
    assert budget["count"] == 105 and len(budget["rows"]) == 5
    assert {r["amount"] for r in budget["charts"]["groups"] if r["year"] == 2026} == {
        5565
    }


def test_chart_bins_and_drill_filters_keep_negative_zero_and_precise_amounts(database):
    rows, _ = normalized_rows()
    amounts = [
        Decimal("-0.123456789"),
        Decimal(0),
        Decimal(1000000),
        Decimal(5000000),
        Decimal(10000000),
        Decimal(50000000),
        Decimal(100000000),
    ]
    with connect() as conn:
        batch.replace_rows(
            conn,
            [
                dict(rows[2], amount=a, category="구독", subcategory="연간")
                for a in amounts
            ],
        )
    data = dashboard_data(2026, bucket="구독", subcategory="연간")
    assert data["count"] == 7
    assert data["rows"][-1]["amount"] == amounts[0]
    assert [r["bin"] for r in data["charts"]["sizes"]] == list(range(7))
    assert data["charts"]["sizes"][0]["amount"] == amounts[0]
    assert dashboard_data(2026, bucket="구독", subcategory="월간")["count"] == 0
    assert dashboard_data(2026, bucket="' OR true --")["count"] == 0
    assert dashboard_data(2026, "school_level", bucket="미기재")["count"] == 7
    assert dashboard_data(2026, "budget_sources", bucket="미기재")["count"] == 7
