"""
Google Drive API 래퍼 함수

읽기 전용이다. 공유 드라이브와 서비스 계정에 공유된 폴더를 함께 조회하므로
모든 호출에 supportsAllDrives를 지정한다.
"""

from googleapiclient.discovery import build

from . import google_auth

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FILE_FIELDS = "id, name, mimeType, modifiedTime, size, webViewLink, parents"


def get_drive_service():
    """운영봇 서비스 계정으로 Drive v3 서비스를 생성한다."""
    creds = google_auth.operate_credentials(SCOPES)
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
