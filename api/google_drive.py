"""
Google Drive API 래퍼 함수

서비스 계정으로 공유 드라이브(Shared Drive)에 접근한다.
서비스 계정은 자체 스토리지 할당량이 없어 개인 My Drive에는 파일을 생성할 수 없다
(403 storageQuotaExceeded). 따라서 파일 생성은 반드시 공유 드라이브 하위에서 수행한다.
"""

import io
import json
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]

FILE_FIELDS = "id, name, mimeType, modifiedTime, size, webViewLink, parents"


def get_drive_service():
    """환경 변수의 서비스 계정 JSON으로 Drive v3 서비스를 생성한다."""
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_files(query: str, page_size: int = 20) -> dict:
    """
    Drive 파일을 검색한다.

    Args:
        query: Drive API의 q 파라미터 (https://developers.google.com/workspace/drive/api/guides/search-files)
        page_size: 최대 결과 수

    Returns:
        Drive API files.list 원본 응답
    """
    service = get_drive_service()
    return (
        service.files()
        .list(
            q=query,
            pageSize=page_size,
            fields=f"files({FILE_FIELDS})",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        )
        .execute()
    )


def get_file_metadata(file_id: str) -> dict:
    """파일 메타데이터를 조회한다."""
    service = get_drive_service()
    return (
        service.files()
        .get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True)
        .execute()
    )


def download_file(file_id: str) -> bytes:
    """바이너리 파일 원본을 내려받는다. Google 네이티브 문서에는 사용할 수 없다."""
    service = get_drive_service()
    return service.files().get_media(fileId=file_id).execute()


def export_file(file_id: str, mime_type: str) -> bytes:
    """Google 네이티브 문서를 지정한 MIME 타입으로 변환하여 내려받는다."""
    service = get_drive_service()
    return service.files().export(fileId=file_id, mimeType=mime_type).execute()


def create_file(
    name: str,
    parent_id: str,
    content: str,
    source_mime_type: str,
    target_mime_type: str | None = None,
) -> dict:
    """
    파일을 생성한다.

    Args:
        name: 파일 이름
        parent_id: 상위 폴더 ID (공유 드라이브 하위여야 한다)
        content: 파일 본문
        source_mime_type: 업로드할 본문의 MIME 타입 (예: text/markdown)
        target_mime_type: 지정하면 Drive가 해당 타입으로 변환하여 저장한다
            (예: application/vnd.google-apps.document)

    Returns:
        Drive API files.create 원본 응답
    """
    service = get_drive_service()

    body: dict = {"name": name, "parents": [parent_id]}
    if target_mime_type:
        body["mimeType"] = target_mime_type

    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")), mimetype=source_mime_type
    )
    return (
        service.files()
        .create(
            body=body,
            media_body=media,
            fields=FILE_FIELDS,
            supportsAllDrives=True,
        )
        .execute()
    )


def update_file(file_id: str, content: str, source_mime_type: str) -> dict:
    """기존 파일의 본문을 덮어쓴다."""
    service = get_drive_service()
    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")), mimetype=source_mime_type
    )
    return (
        service.files()
        .update(
            fileId=file_id,
            media_body=media,
            fields=FILE_FIELDS,
            supportsAllDrives=True,
        )
        .execute()
    )
