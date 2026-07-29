"""
Google Drive 도구 테스트
"""

import pytest
from unittest.mock import patch

from app.tools import drive_scope, drive_tools

ROOT = "root-folder"
SUB = "sub-folder"


def _tools(root_folder_id=None):
    search, read = drive_tools.get_drive_tools(root_folder_id)
    return {"search": search, "read": read}


@pytest.fixture(autouse=True)
def _clear_scope_cache():
    drive_scope._scope_cache.clear()
    yield
    drive_scope._scope_cache.clear()


class TestReadDriveFile:
    """파일 유형별 본문 추출 분기"""

    @pytest.mark.asyncio
    async def test_google_doc_is_exported_as_markdown(self):
        """Google 문서는 마크다운으로 export 되어야 한다"""
        with (
            patch.object(drive_tools.google_drive, "get_file_metadata") as mock_meta,
            patch.object(drive_tools.google_drive, "export_file") as mock_export,
        ):
            mock_meta.return_value = {
                "name": "기획서",
                "mimeType": "application/vnd.google-apps.document",
            }
            mock_export.return_value = "# 제목\n본문".encode("utf-8")

            result = await _tools()["read"].ainvoke({"file_id": "doc-1"})

        mock_export.assert_called_once_with("doc-1", "text/markdown")
        assert "기획서" in result
        assert "본문" in result

    @pytest.mark.asyncio
    async def test_spreadsheet_is_exported_as_csv(self):
        """스프레드시트는 CSV로 export 되어야 한다"""
        with (
            patch.object(drive_tools.google_drive, "get_file_metadata") as mock_meta,
            patch.object(drive_tools.google_drive, "export_file") as mock_export,
        ):
            mock_meta.return_value = {
                "name": "매출",
                "mimeType": "application/vnd.google-apps.spreadsheet",
            }
            mock_export.return_value = b"a,b\n1,2"

            result = await _tools()["read"].ainvoke({"file_id": "sheet-1"})

        mock_export.assert_called_once_with("sheet-1", "text/csv")
        assert "a,b" in result

    @pytest.mark.asyncio
    async def test_pdf_goes_through_text_extraction(self):
        """PDF는 다운로드 후 텍스트 추출 경로를 타야 한다"""
        with (
            patch.object(drive_tools.google_drive, "get_file_metadata") as mock_meta,
            patch.object(drive_tools.google_drive, "download_file") as mock_download,
            patch.object(drive_tools, "_extract_pdf_text") as mock_extract,
        ):
            mock_meta.return_value = {
                "name": "계약서.pdf",
                "mimeType": "application/pdf",
            }
            mock_download.return_value = b"%PDF-1.4 ..."
            mock_extract.return_value = "추출된 본문"

            result = await _tools()["read"].ainvoke({"file_id": "pdf-1"})

        mock_extract.assert_called_once_with(b"%PDF-1.4 ...")
        assert "추출된 본문" in result

    @pytest.mark.asyncio
    async def test_unsupported_type_reports_without_downloading(self):
        """읽을 수 없는 형식은 다운로드하지 않고 안내만 돌려준다"""
        with (
            patch.object(drive_tools.google_drive, "get_file_metadata") as mock_meta,
            patch.object(drive_tools.google_drive, "download_file") as mock_download,
            patch.object(drive_tools.google_drive, "export_file") as mock_export,
        ):
            mock_meta.return_value = {"name": "사진.png", "mimeType": "image/png"}

            result = await _tools()["read"].ainvoke({"file_id": "img-1"})

        mock_download.assert_not_called()
        mock_export.assert_not_called()
        assert "읽을 수 없는 형식" in result

    @pytest.mark.asyncio
    async def test_long_content_is_truncated_with_notice(self):
        """긴 본문은 잘리고 생략 사실이 표시되어야 한다"""
        long_text = "가" * (drive_tools.MAX_CONTENT_CHARS + 500)

        with (
            patch.object(drive_tools.google_drive, "get_file_metadata") as mock_meta,
            patch.object(drive_tools.google_drive, "export_file") as mock_export,
        ):
            mock_meta.return_value = {
                "name": "긴문서",
                "mimeType": "application/vnd.google-apps.document",
            }
            mock_export.return_value = long_text.encode("utf-8")

            result = await _tools()["read"].ainvoke({"file_id": "doc-2"})

        assert "500자 생략됨" in result


class TestFolderScope:
    """작업 공간 하위 트리 수집"""

    def test_scope_collects_subfolders_and_caches(self):
        """하위 폴더를 모으고, 두 번째 호출은 캐시를 쓴다"""
        with patch.object(drive_scope.google_drive, "list_files") as mock_list:
            mock_list.side_effect = [
                {"files": [{"id": SUB}]},  # 루트의 자식
                {"files": []},  # SUB의 자식
            ]
            first = drive_scope.folder_scope(ROOT)
            second = drive_scope.folder_scope(ROOT)

        assert first == [ROOT, SUB]
        assert second == [ROOT, SUB]
        assert mock_list.call_count == 2  # 두 번째는 캐시

    def test_scope_stops_at_depth_limit(self):
        """깊이 상한에서 멈춘다"""
        with patch.object(drive_scope.google_drive, "list_files") as mock_list:
            # 매 단계마다 자식이 하나씩 계속 나오는 무한 트리
            mock_list.side_effect = [{"files": [{"id": f"f{i}"}]} for i in range(20)]
            result = drive_scope.folder_scope(ROOT)

        assert len(result) == drive_scope.MAX_SCOPE_DEPTH + 1


class TestScopedAccess:
    """범위 강제는 프롬프트가 아니라 코드에서 건다"""

    def _patch_scope(self):
        return patch.object(drive_scope, "folder_scope", return_value=[ROOT, SUB])

    @pytest.mark.asyncio
    async def test_search_is_narrowed_to_workspace(self):
        """검색 쿼리에 작업 공간 조건이 자동으로 붙는다"""
        with (
            self._patch_scope(),
            patch.object(drive_tools.google_drive, "list_files") as mock_list,
        ):
            mock_list.return_value = {"files": []}
            await _tools(ROOT)["search"].ainvoke({"query": "name contains '기획'"})

        sent_query = mock_list.call_args.args[0]
        assert f"'{ROOT}' in parents" in sent_query
        assert f"'{SUB}' in parents" in sent_query
        assert "name contains '기획'" in sent_query

    @pytest.mark.asyncio
    async def test_search_is_unchanged_without_workspace(self):
        """작업 공간이 없으면 쿼리를 그대로 보낸다"""
        with patch.object(drive_tools.google_drive, "list_files") as mock_list:
            mock_list.return_value = {"files": []}
            await _tools()["search"].ainvoke({"query": "name contains '기획'"})

        assert mock_list.call_args.args[0] == "name contains '기획'"

    @pytest.mark.asyncio
    async def test_reading_outside_workspace_is_refused(self):
        """범위 밖 파일은 읽지 않는다"""
        with (
            self._patch_scope(),
            patch.object(drive_tools.google_drive, "get_file_metadata") as mock_meta,
            patch.object(drive_tools.google_drive, "export_file") as mock_export,
        ):
            mock_meta.return_value = {
                "name": "남의 문서",
                "mimeType": "application/vnd.google-apps.document",
                "parents": ["다른-폴더"],
            }
            result = await _tools(ROOT)["read"].ainvoke({"file_id": "x"})

        mock_export.assert_not_called()
        assert "작업 공간 밖" in result

    @pytest.mark.asyncio
    async def test_reading_inside_workspace_is_allowed(self):
        """범위 안 파일은 정상적으로 읽는다"""
        with (
            self._patch_scope(),
            patch.object(drive_tools.google_drive, "get_file_metadata") as mock_meta,
            patch.object(drive_tools.google_drive, "export_file") as mock_export,
        ):
            mock_meta.return_value = {
                "name": "우리 문서",
                "mimeType": "application/vnd.google-apps.document",
                "parents": [SUB],
            }
            mock_export.return_value = "본문".encode("utf-8")
            result = await _tools(ROOT)["read"].ainvoke({"file_id": "x"})

        assert "우리 문서" in result


class TestSearchResults:
    """검색 결과 포맷"""

    @pytest.mark.asyncio
    async def test_empty_result_guides_retry(self):
        """결과가 없으면 재시도를 유도하는 문구를 돌려준다"""
        with patch.object(drive_tools.google_drive, "list_files") as mock_list:
            mock_list.return_value = {"files": []}
            result = await _tools()["search"].ainvoke({"query": "name = 'x'"})

        assert "검색 결과가 없습니다" in result

    @pytest.mark.asyncio
    async def test_result_includes_id_for_follow_up_read(self):
        """검색 결과에는 이어서 읽을 수 있도록 file_id가 포함되어야 한다"""
        with patch.object(drive_tools.google_drive, "list_files") as mock_list:
            mock_list.return_value = {
                "files": [
                    {
                        "id": "abc123",
                        "name": "2026 연수 계획",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-07-01T00:00:00Z",
                    }
                ]
            }
            result = await _tools()["search"].ainvoke({"query": "name contains '연수'"})

        assert "id=abc123" in result
        assert "2026 연수 계획" in result


class TestNormalizeFolderId:
    """환경 변수에 링크를 그대로 넣어도 되게 한다"""

    def test_accepts_browser_copied_link(self):
        """브라우저에서 복사한 ?usp= 꼬리표가 붙은 링크"""
        url = "https://drive.google.com/drive/folders/1Ca4MSIv8IimJ2wg?usp=drive_link"
        assert drive_scope.normalize_folder_id(url) == "1Ca4MSIv8IimJ2wg"

    def test_accepts_user_scoped_link(self):
        url = "https://drive.google.com/drive/u/0/folders/XYZ789"
        assert drive_scope.normalize_folder_id(url) == "XYZ789"

    def test_accepts_bare_id(self):
        assert drive_scope.normalize_folder_id("1Ca4MSIv8IimJ2wg") == "1Ca4MSIv8IimJ2wg"

    def test_strips_whitespace(self):
        assert drive_scope.normalize_folder_id("  ABC123  ") == "ABC123"

    def test_empty_is_none(self):
        assert drive_scope.normalize_folder_id("") is None
        assert drive_scope.normalize_folder_id("   ") is None
        assert drive_scope.normalize_folder_id(None) is None
