from typing import Any

from gspread.utils import absolute_range_name

from api.google_sheets import (
    DEFAULT_ACCOUNT,
    get_spreadsheet_metadata,
    get_spreadsheet_values_batch,
)


def fetch_ledger(sources: dict[str, Any], account: str = DEFAULT_ACCOUNT) -> dict:
    led = sources["ledger"]
    meta = get_spreadsheet_metadata(led["spreadsheet_id"], account=account)
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

    ranges = [absolute_range_name(tab, led["range"]) for tab in led["tabs"]]
    if tax_tab:
        ranges.append(absolute_range_name(tax_tab["name"], tax_tab["range"]))

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
        "tabs": tabs_out,
        "taxonomy_rows": taxonomy_rows,
    }
