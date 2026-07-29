"""
Google Sheets 조회 LangChain Tools

drive_tools는 파일을 통째로 읽는다.
여기 있는 도구는 셀 범위 단위로 시트 내부를 읽는다.

시트 쓰기는 아직 두지 않았다. 되돌리기가 어렵고, 서식·정렬·필터까지 다루려면
batchUpdate 요청을 그대로 노출해야 하는데 그 파괴 범위를 먼저 합의해야 한다.
"""

import asyncio
from typing import Annotated

from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from langchain_core.tools import tool

from api import google_sheets

# 한 번에 돌려줄 최대 행 수 (에이전트 컨텍스트 보호)
MAX_ROWS = 200

SHEET_ERRORS = (APIError, SpreadsheetNotFound, WorksheetNotFound)


def _format_worksheets(worksheets: list[dict]) -> str:
    lines = ["탭 목록 (범위를 지정해 다시 호출하세요):"]
    for ws in worksheets:
        lines.append(f"- {ws['title']} ({ws['row_count']}행 × {ws['col_count']}열)")
    return "\n".join(lines)


def _format_rows(rows: list[list]) -> str:
    if not rows:
        return "범위에 값이 없습니다."

    truncated = rows[:MAX_ROWS]
    lines = [" | ".join(str(cell) for cell in row) for row in truncated]

    if len(rows) > MAX_ROWS:
        lines.append(
            f"[...{len(rows) - MAX_ROWS}행 생략됨. 범위를 좁혀 다시 호출하세요.]"
        )
    return "\n".join(lines)


@tool
async def read_sheet_range(
    spreadsheet_id: Annotated[str, "스프레드시트 파일 ID"],
    range_a1: Annotated[
        str | None,
        "'시트1!A1:D20' 형태의 범위. 생략하면 탭 목록과 크기를 돌려줍니다.",
    ] = None,
) -> str:
    """
    Google 스프레드시트의 특정 범위를 읽습니다.

    read_drive_file은 시트를 통째로 CSV로 읽지만, 이 도구는 원하는 범위만 읽습니다.
    큰 시트를 다룰 때는 이 도구를 쓰세요.

    어떤 탭이 있는지 모르면 range_a1 없이 먼저 호출해 구조를 파악한 뒤,
    범위를 지정해 다시 호출하세요.

    Returns:
        str: 탭 목록 또는 범위의 셀 값
    """
    try:
        if range_a1 is None:
            worksheets = await asyncio.to_thread(
                google_sheets.list_worksheets, spreadsheet_id
            )
            return _format_worksheets(worksheets)

        response = await asyncio.to_thread(
            google_sheets.get_range, spreadsheet_id, range_a1
        )
    except SHEET_ERRORS as e:
        return f"시트 읽기 실패: {e}. 파일 ID와 탭 이름, 범위 표기를 확인하세요."

    return _format_rows(response.get("values", []))
