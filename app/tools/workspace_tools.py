"""
Google Sheets / Docs 네이티브 조작 LangChain Tools

drive_tools는 파일을 통째로 읽고 쓰는 도구다.
여기 있는 도구들은 셀 범위나 특정 문구처럼 문서 내부를 직접 다룬다.
"""

import asyncio
from typing import Annotated

from googleapiclient.errors import HttpError
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from langchain_core.tools import tool

from api import google_docs, google_sheets

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


@tool
async def update_sheet_range(
    spreadsheet_id: Annotated[str, "스프레드시트 파일 ID"],
    range_a1: Annotated[str, "'시트1!A1:D20' 형태의 범위"],
    values: Annotated[
        list[list[str]],
        "행 단위 2차원 배열. 예: [['이름', '수량'], ['연필', '3']]",
    ],
) -> str:
    """
    Google 스프레드시트의 특정 범위에 값을 씁니다.

    지정한 범위의 기존 값은 대체됩니다. 어떤 셀이 바뀌는지 사용자에게 알리고
    확인을 받은 뒤에 호출하세요.

    맨 아래에 행을 덧붙이려면 read_sheet_range로 마지막 행을 확인한 뒤
    그 다음 행부터 범위를 지정하세요.

    Returns:
        str: 갱신된 셀 범위와 개수
    """
    try:
        response = await asyncio.to_thread(
            google_sheets.update_range, spreadsheet_id, range_a1, values
        )
    except SHEET_ERRORS as e:
        return (
            f"시트 쓰기 실패: {e}. 서비스 계정에 편집 권한이 있는지 확인이 필요합니다."
        )

    updated_range = response.get("updatedRange", range_a1)
    updated_cells = response.get("updatedCells", 0)
    return f"{updated_range}에 {updated_cells}개 셀을 썼습니다."


@tool
async def replace_text_in_doc(
    document_id: Annotated[str, "Google 문서 파일 ID"],
    find: Annotated[str, "찾을 문자열"],
    replace: Annotated[str, "바꿀 문자열"],
) -> str:
    """
    Google 문서에서 일치하는 모든 문구를 치환합니다.

    문서 일부만 고칠 때 write_drive_file로 전체를 덮어쓰면 서식이 사라지므로,
    문구 단위 수정에는 이 도구를 쓰세요.

    치환은 되돌릴 수 없습니다. 무엇을 무엇으로 바꿀지 사용자에게 알리고
    확인을 받은 뒤에 호출하세요.

    Returns:
        str: 치환된 횟수
    """
    try:
        response = await asyncio.to_thread(
            google_docs.replace_all_text, document_id, find, replace
        )
    except HttpError as e:
        return f"문서 치환 실패: {e}. 파일 ID와 편집 권한을 확인하세요."

    replies = response.get("replies", [])
    occurrences = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)

    if occurrences == 0:
        return f"'{find}'을(를) 찾지 못해 아무것도 바꾸지 않았습니다."
    return f"'{find}' → '{replace}' {occurrences}곳을 바꿨습니다."
