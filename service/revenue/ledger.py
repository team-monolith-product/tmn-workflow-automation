"""
매출장 구글시트를 통째로 읽어 raw 스냅샷으로 만든다.

호출 수는 시트 하나에 2회다(메타데이터 1 + values.batchGet 1). 탭마다
get_worksheet_values 를 부르면 탭당 3회씩 든다.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from api.google_sheets import (
    DEFAULT_ACCOUNT,
    get_spreadsheet_metadata,
    get_spreadsheet_values_batch,
)

KST = timezone(timedelta(hours=9))


def fetch_ledger(sources: dict[str, Any], account: str = DEFAULT_ACCOUNT) -> dict:
    """매출장을 읽어 raw 스냅샷을 돌려준다.

    탭 이름이 바뀌면 조용히 빈 값을 집계하는 대신 여기서 터진다. 매출장은 사람이
    고치는 시트라 탭 이름이 바뀌는 일이 실제로 있었다(2026-09-10 (구)·(신) 분리).

    Args:
        sources: knowledge/revenue/sources.yml
        account: 어느 서비스 계정으로 읽을지. 매출장은 DEFAULT 계정에만 공유한다

    Returns:
        dict: fetched_at, spreadsheet_id, spreadsheet_title, tabs{탭: {fiscal_year,
            columns, rows}}, taxonomy_tab{name, rows}

    Raises:
        ValueError: sources.yml 이 가리키는 탭이 시트에 없을 때
    """
    led = sources["ledger"]
    meta = get_spreadsheet_metadata(led["spreadsheet_id"], account=account)
    title = meta.get("properties", {}).get("title", "")
    live_tabs = {sheet["properties"]["title"] for sheet in meta.get("sheets", [])}

    missing = [tab for tab in led["tabs"] if tab not in live_tabs]
    if missing:
        raise ValueError(
            f"매출장에 탭이 없다: {missing} / 실제 탭: {sorted(live_tabs)}."
            " 탭 이름이 바뀌었으면 knowledge/revenue/sources.yml 의 ledger.tabs 를 고친다."
        )

    tax_tab = led.get("taxonomy_tab") or {}
    if tax_tab and tax_tab["name"] not in live_tabs:
        raise ValueError(
            f"분류마스터 탭이 없다: {tax_tab['name']} / 실제 탭: {sorted(live_tabs)}."
            " 이름이 바뀌었으면 sources.yml 의 ledger.taxonomy_tab.name 을 고친다."
        )

    # 탭 이름 안의 작은따옴표는 A1 표기에서 두 번 겹쳐 쓴다.
    ranges = [
        f"'{tab.replace(chr(39), chr(39) * 2)}'!{led['range']}" for tab in led["tabs"]
    ]
    if tax_tab:
        ranges.append(f"'{tax_tab['name']}'!{tax_tab['range']}")

    # UNFORMATTED_VALUE 라야 공급가액이 "9,090,909" 가 아니라 숫자로 온다.
    # 그때 날짜 셀은 일련번호가 되므로 FORMATTED_STRING 으로 문자열을 유지한다.
    payload = get_spreadsheet_values_batch(
        led["spreadsheet_id"],
        ranges,
        value_render_option="UNFORMATTED_VALUE",
        date_time_render_option="FORMATTED_STRING",
        account=account,
    )
    value_ranges = payload.get("valueRanges", [])
    if len(value_ranges) != len(ranges):
        raise ValueError(
            f"batchGet 응답이 요청과 다르다: 요청 {len(ranges)} / 응답 {len(value_ranges)}"
        )

    taxonomy_rows = value_ranges.pop().get("values", []) if tax_tab else []

    tabs_out = {}
    for tab, value_range in zip(led["tabs"], value_ranges, strict=True):
        spec = led["tabs"][tab]
        spec = spec if isinstance(spec, dict) else {"year": spec}
        tabs_out[tab] = {
            "fiscal_year": spec["year"],
            "columns": spec.get("columns", {}),
            "rows": value_range.get("values", []),
        }

    return {
        "fetched_at": datetime.now(KST).isoformat(timespec="seconds"),
        "spreadsheet_id": led["spreadsheet_id"],
        "spreadsheet_title": title,
        "tabs": tabs_out,
        "taxonomy_tab": (
            {"name": tax_tab["name"], "rows": taxonomy_rows} if tax_tab else None
        ),
    }
