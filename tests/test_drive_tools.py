"""
Google Drive 도구 테스트
"""

import pytest
from unittest.mock import patch

from app.tools import drive_tools


class TestReadDriveFile:
    """파일 유형별 본문 추출 분기 테스트"""

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

            result = await drive_tools.read_drive_file.ainvoke({"file_id": "doc-1"})

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

            result = await drive_tools.read_drive_file.ainvoke({"file_id": "sheet-1"})

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

            result = await drive_tools.read_drive_file.ainvoke({"file_id": "pdf-1"})

        mock_extract.assert_called_once_with(b"%PDF-1.4 ...")
        assert "추출된 본문" in result

    @pytest.mark.asyncio
    async def test_plain_text_is_downloaded_directly(self):
        """텍스트 계열은 변환 없이 그대로 내려받는다"""
        with (
            patch.object(drive_tools.google_drive, "get_file_metadata") as mock_meta,
            patch.object(drive_tools.google_drive, "download_file") as mock_download,
            patch.object(drive_tools.google_drive, "export_file") as mock_export,
        ):
            mock_meta.return_value = {"name": "메모.txt", "mimeType": "text/plain"}
            mock_download.return_value = "그냥 텍스트".encode("utf-8")

            result = await drive_tools.read_drive_file.ainvoke({"file_id": "txt-1"})

        mock_export.assert_not_called()
        assert "그냥 텍스트" in result

    @pytest.mark.asyncio
    async def test_unsupported_type_reports_without_downloading(self):
        """읽을 수 없는 형식은 다운로드하지 않고 안내만 돌려준다"""
        with (
            patch.object(drive_tools.google_drive, "get_file_metadata") as mock_meta,
            patch.object(drive_tools.google_drive, "download_file") as mock_download,
            patch.object(drive_tools.google_drive, "export_file") as mock_export,
        ):
            mock_meta.return_value = {"name": "사진.png", "mimeType": "image/png"}

            result = await drive_tools.read_drive_file.ainvoke({"file_id": "img-1"})

        mock_download.assert_not_called()
        mock_export.assert_not_called()
        assert "읽을 수 없는 형식" in result
        assert "image/png" in result

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

            result = await drive_tools.read_drive_file.ainvoke({"file_id": "doc-2"})

        assert "500자 생략됨" in result
        assert len(result) < len(long_text)


class TestWriteDriveFile:
    """생성/덮어쓰기 분기 테스트"""

    @pytest.mark.asyncio
    async def test_file_id_triggers_update_not_create(self):
        """file_id가 있으면 덮어쓰기만 수행한다"""
        with (
            patch.object(drive_tools.google_drive, "update_file") as mock_update,
            patch.object(drive_tools.google_drive, "create_file") as mock_create,
        ):
            mock_update.return_value = {
                "name": "회의록",
                "webViewLink": "https://drive.example/1",
            }

            result = await drive_tools.write_drive_file.ainvoke(
                {"name": "회의록", "content": "새 본문", "file_id": "doc-9"}
            )

        mock_create.assert_not_called()
        mock_update.assert_called_once_with("doc-9", "새 본문", "text/markdown")
        assert "덮어썼습니다" in result

    @pytest.mark.asyncio
    async def test_new_file_is_created_as_google_doc_in_given_folder(self):
        """file_id가 없으면 지정 폴더에 Google 문서로 생성한다"""
        with (
            patch.object(drive_tools.google_drive, "create_file") as mock_create,
            patch.object(drive_tools.google_drive, "update_file") as mock_update,
        ):
            mock_create.return_value = {
                "name": "신규",
                "webViewLink": "https://drive.example/2",
            }

            result = await drive_tools.write_drive_file.ainvoke(
                {"name": "신규", "content": "# 제목", "folder_id": "folder-1"}
            )

        mock_update.assert_not_called()
        mock_create.assert_called_once_with(
            "신규",
            "folder-1",
            "# 제목",
            "text/markdown",
            drive_tools.GOOGLE_DOC_MIME_TYPE,
        )
        assert "생성했습니다" in result

    @pytest.mark.asyncio
    async def test_as_google_doc_false_skips_conversion(self):
        """as_google_doc=False면 변환 타입을 넘기지 않는다"""
        with patch.object(drive_tools.google_drive, "create_file") as mock_create:
            mock_create.return_value = {"name": "raw.md", "webViewLink": ""}

            await drive_tools.write_drive_file.ainvoke(
                {
                    "name": "raw.md",
                    "content": "본문",
                    "folder_id": "folder-1",
                    "as_google_doc": False,
                }
            )

        assert mock_create.call_args.args[4] is None

    @pytest.mark.asyncio
    async def test_missing_folder_returns_guidance_without_api_call(self, monkeypatch):
        """대상 폴더를 알 수 없으면 API를 호출하지 않고 안내한다"""
        monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_ID", raising=False)

        with patch.object(drive_tools.google_drive, "create_file") as mock_create:
            result = await drive_tools.write_drive_file.ainvoke(
                {"name": "무소속", "content": "본문"}
            )

        mock_create.assert_not_called()
        assert "폴더" in result

    @pytest.mark.asyncio
    async def test_env_folder_is_used_as_default(self, monkeypatch):
        """folder_id 생략 시 환경 변수의 기본 폴더를 쓴다"""
        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "env-folder")

        with patch.object(drive_tools.google_drive, "create_file") as mock_create:
            mock_create.return_value = {"name": "기본", "webViewLink": ""}

            await drive_tools.write_drive_file.ainvoke(
                {"name": "기본", "content": "본문"}
            )

        assert mock_create.call_args.args[1] == "env-folder"


class TestSearchDriveFiles:
    """검색 결과 포맷 테스트"""

    @pytest.mark.asyncio
    async def test_empty_result_guides_retry(self):
        """결과가 없으면 재시도를 유도하는 문구를 돌려준다"""
        with patch.object(drive_tools.google_drive, "list_files") as mock_list:
            mock_list.return_value = {"files": []}

            result = await drive_tools.search_drive_files.ainvoke(
                {"query": "name = 'x'"}
            )

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

            result = await drive_tools.search_drive_files.ainvoke(
                {"query": "name contains '연수'"}
            )

        assert "id=abc123" in result
        assert "2026 연수 계획" in result
