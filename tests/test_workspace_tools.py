"""
Google Sheets 조회 도구 테스트
"""

import pytest
from unittest.mock import patch

from app.tools import workspace_tools


class TestReadSheetRange:
    """범위 읽기와 탭 탐색"""

    @pytest.mark.asyncio
    async def test_without_range_returns_tab_list(self):
        """범위를 생략하면 탭 목록을 돌려주고 값은 읽지 않는다"""
        with (
            patch.object(workspace_tools.google_sheets, "list_worksheets") as mock_list,
            patch.object(workspace_tools.google_sheets, "get_range") as mock_get,
        ):
            mock_list.return_value = [
                {"title": "응답", "id": 0, "row_count": 500, "col_count": 8},
                {"title": "집계", "id": 1, "row_count": 20, "col_count": 4},
            ]

            result = await workspace_tools.read_sheet_range.ainvoke(
                {"spreadsheet_id": "sheet-1"}
            )

        mock_get.assert_not_called()
        assert "응답" in result
        assert "500행 × 8열" in result

    @pytest.mark.asyncio
    async def test_with_range_returns_values(self):
        """범위를 주면 해당 범위만 읽는다"""
        with patch.object(workspace_tools.google_sheets, "get_range") as mock_get:
            mock_get.return_value = {"values": [["이름", "수량"], ["연필", "3"]]}

            result = await workspace_tools.read_sheet_range.ainvoke(
                {"spreadsheet_id": "sheet-1", "range_a1": "응답!A1:B2"}
            )

        mock_get.assert_called_once_with("sheet-1", "응답!A1:B2")
        assert "이름 | 수량" in result
        assert "연필 | 3" in result

    @pytest.mark.asyncio
    async def test_rows_are_truncated_with_notice(self):
        """행이 많으면 잘리고 생략 사실을 알린다"""
        rows = [[str(i)] for i in range(workspace_tools.MAX_ROWS + 30)]

        with patch.object(workspace_tools.google_sheets, "get_range") as mock_get:
            mock_get.return_value = {"values": rows}

            result = await workspace_tools.read_sheet_range.ainvoke(
                {"spreadsheet_id": "sheet-1", "range_a1": "시트1!A1:A300"}
            )

        assert "30행 생략됨" in result

    @pytest.mark.asyncio
    async def test_empty_range_is_reported(self):
        """빈 범위는 값이 없다고 알린다"""
        with patch.object(workspace_tools.google_sheets, "get_range") as mock_get:
            mock_get.return_value = {}

            result = await workspace_tools.read_sheet_range.ainvoke(
                {"spreadsheet_id": "sheet-1", "range_a1": "시트1!Z1:Z9"}
            )

        assert "값이 없습니다" in result
