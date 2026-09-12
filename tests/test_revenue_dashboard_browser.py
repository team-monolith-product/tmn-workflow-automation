import os
from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.revenue import web
from revenue_fixture import normalized_rows
from scripts.sync_revenue_ledger import replace_rows
from service.db import connect
from test_revenue_db import database


def test_original_dashboard_interactions_with_database_paging(database, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    rows, _ = normalized_rows()
    with connect() as conn:
        replace_rows(
            conn,
            rows[:2]
            + [
                dict(
                    rows[2],
                    customer=f"학교 {i:03d}",
                    item=f"구독 {i:03d}",
                    amount=Decimal("1000000.123456789") + i,
                    category="구독",
                    subcategory="연간",
                    school_level="고등학교",
                    budget_sources=["자체", "정책", "자체"],
                    program_name="교육 지원",
                    program_budget=Decimal("100000000.123456789"),
                )
                for i in range(105)
            ]
            + [dict(rows[3], category="연수", subcategory=None), rows[-1]],
        )
    monkeypatch.setattr(
        web, "get_me", AsyncMock(return_value={"email": "test@example.com"})
    )
    app = FastAPI()
    app.include_router(web.router)
    with TestClient(app) as client, playwright.sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            headless=True,
        )
        page = browser.new_page(
            viewport={"width": 1440, "height": 1000}, reduced_motion="reduce"
        )
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        requests = []

        def serve(route):
            url = urlsplit(route.request.url)
            requests.append(url.query)
            response = client.get(
                url.path + ("?" + url.query if url.query else ""),
                headers={"cookie": "revenue_session=test"},
            )
            route.fulfill(
                status=response.status_code,
                body=response.content,
                content_type=response.headers.get("content-type", "text/plain"),
            )

        page.route("http://revenue.test/**", serve)
        page.goto("http://revenue.test/revenue/")
        expect = playwright.expect
        expect(page.locator(".tile")).to_have_count(4)
        expect(page.locator("#pipeTable tbody tr")).to_have_count(1)
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        page.locator("#themeBtn").click()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        page.locator("#themeBtn").click()
        page.locator('#mMode [data-mode="cum"]').click()
        expect(page.locator("#monthlyCum svg")).to_be_visible()
        page.locator('#mMode [data-mode="table"]').click()
        expect(page.locator("#monthlyTable tbody tr")).to_have_count(12)
        page.locator('#cMode [data-mode="tmap"]').click()
        expect(page.locator("#tmap .tm")).to_have_count(2)
        page.locator('#tmap [data-n="구독"]').click()
        page.locator("#cats [data-s]").first.click()
        expect(page.locator("#cats [data-detail] tbody tr")).to_have_count(100)
        expect(page.locator("#cats [data-detail]")).to_contain_text(
            "100,000,000.123456789"
        )
        page.locator("#cats [data-detail]").get_by_role("button", name="다음").click()
        expect(page.locator("#cats [data-detail] tbody tr")).to_have_count(5)
        page.locator("#q").fill("학교 000")
        expect(page.locator("#warnings")).to_be_empty()
        expect(page.locator("#cats [data-b]")).to_have_count(1)
        page.locator("#cats [data-b]").click()
        page.locator("#cats [data-s]").click()
        expect(page.locator("#cats [data-detail] tbody tr")).to_have_count(1)
        expect(page.locator("#cats [data-detail]")).to_contain_text(
            "1,000,000.123456789"
        )
        page.locator("#q").fill("없는 고객")
        expect(page.locator("#cats")).to_contain_text("검색어와 맞는 거래가 없다")
        page.locator("#q").fill("")
        expect(page.locator("#cats [data-b]")).to_have_count(2)
        page.locator('#viewTabs [data-v="budget_sources"]').click()
        expect(page.locator("#viewNote")).to_contain_text("겹치므로")
        page.locator('#cats [data-b="자체"]').click()
        expect(page.locator("#cats [data-detail] tbody tr")).to_have_count(100)
        page.locator('#yearTabs [data-y="2025"]').click()
        expect(page.locator('#yearTabs [data-y="2025"]')).to_have_attribute(
            "aria-pressed", "true"
        )
        expect(page.locator("#pipePanel")).to_be_hidden()
        page.locator('#yearTabs [data-y="2026"]').click()
        expect(page.locator("#pipePanel")).to_be_visible()
        page.locator('#viewTabs [data-v="category"]').click()
        expect(page.locator('#viewTabs [data-v="category"]')).to_have_attribute(
            "aria-pressed", "true"
        )
        page.locator('#mMode [data-mode="bars"]').click()
        page.screenshot(path="/tmp/revenue-restored-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="/tmp/revenue-restored-mobile.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#themeBtn").click()
        page.screenshot(path="/tmp/revenue-restored-dark.png", full_page=True)
        assert any("page=2" in query for query in requests)
        assert not errors, errors
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator("#custBars .hb").first.click()
        expect(page.locator("#q")).to_have_value("학교 104")
        expect(page.locator('#cats [data-b="구독"]')).to_have_attribute(
            "aria-expanded", "true"
        )
        with connect() as conn:
            conn.execute("DELETE FROM revenue_transactions")
        page.goto("http://revenue.test/revenue/")
        expect(page.locator(".tile")).to_have_count(4)
        expect(page.locator("#custStats")).to_contain_text("거래 없음")
        page.locator('#mMode [data-mode="cum"]').click()
        page.locator('#cMode [data-mode="tmap"]').click()
        expect(page.locator("#tmap")).to_contain_text("표시할 항목 없음")
        assert not errors, errors
        browser.close()
