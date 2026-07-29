"""
Google Sheets / Docs 네이티브 조작 도구 테스트
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


class TestUpdateSheetRange:
    """범위 쓰기"""

    @pytest.mark.asyncio
    async def test_passes_values_through_and_reports_count(self):
        """값을 그대로 넘기고 갱신 결과를 알린다"""
        values = [["이름", "수량"], ["연필", "3"]]

        with patch.object(workspace_tools.google_sheets, "update_range") as mock_update:
            mock_update.return_value = {
                "updatedRange": "집계!A1:B2",
                "updatedCells": 4,
            }

            result = await workspace_tools.update_sheet_range.ainvoke(
                {
                    "spreadsheet_id": "sheet-1",
                    "range_a1": "집계!A1:B2",
                    "values": values,
                }
            )

        mock_update.assert_called_once_with("sheet-1", "집계!A1:B2", values)
        assert "집계!A1:B2" in result
        assert "4개 셀" in result


class TestReplaceTextInDoc:
    """문서 문구 치환"""

    @pytest.mark.asyncio
    async def test_reports_number_of_replacements(self):
        """치환 횟수를 알린다"""
        with patch.object(workspace_tools.google_docs, "replace_all_text") as mock_rep:
            mock_rep.return_value = {
                "replies": [{"replaceAllText": {"occurrencesChanged": 3}}]
            }

            result = await workspace_tools.replace_text_in_doc.ainvoke(
                {"document_id": "doc-1", "find": "2025년", "replace": "2026년"}
            )

        mock_rep.assert_called_once_with("doc-1", "2025년", "2026년")
        assert "3곳" in result

    @pytest.mark.asyncio
    async def test_zero_match_is_stated_explicitly(self):
        """일치가 없으면 아무것도 바꾸지 않았음을 분명히 알린다"""
        with patch.object(workspace_tools.google_docs, "replace_all_text") as mock_rep:
            mock_rep.return_value = {
                "replies": [{"replaceAllText": {"occurrencesChanged": 0}}]
            }

            result = await workspace_tools.replace_text_in_doc.ainvoke(
                {"document_id": "doc-1", "find": "없는문구", "replace": "x"}
            )

        assert "찾지 못해" in result
