"""
Google Drive API 래퍼 함수
"""

import json
import os

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from api.google_sheets import OPERATING_ACCOUNT

# 파일을 올려야 하므로 쓰기 스코프다. drive.file 은 앱이 만든 파일만 보이는데,
# 사람이 공유해 준 폴더 안에 올리려면 그 폴더가 보여야 한다.
SCOPES = ["https://www.googleapis.com/auth/drive"]

FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
REQUEST_TIMEOUT = 30
MULTIPART_BOUNDARY = "tmn-workflow-automation-boundary"

_session: AuthorizedSession | None = None


def _get_session() -> AuthorizedSession:
    """Drive API 가 켜진 사업 운영 계정으로 세션을 만들어 재사용한다."""
    global _session
    if _session is None:
        info = json.loads(os.environ[OPERATING_ACCOUNT])
        credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
        _session = AuthorizedSession(credentials)
    return _session


def list_folder_files(folder_id: str) -> list[dict]:
    """
    폴더 안의 파일을 전부 나열한다. 휴지통은 뺀다.

    공유 드라이브 플래그가 필수다. 서비스 계정은 내 드라이브 용량이 없어
    공유 드라이브에만 올릴 수 있고, 플래그 없이 부르면 그쪽 파일이 안 보인다.

    Args:
        folder_id: 폴더 ID

    Returns:
        list[dict]: id·name·appProperties
    """
    params = {
        "q": f"'{folder_id}' in parents and trashed=false",
        "pageSize": 1000,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "fields": "nextPageToken,files(id,name,appProperties)",
    }
    files: list[dict] = []
    while True:
        resp = _get_session().get(FILES_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        files.extend(payload.get("files", []))
        token = payload.get("nextPageToken")
        if not token:
            return files
        params["pageToken"] = token


def create_file(
    folder_id: str,
    name: str,
    content: bytes,
    mime_type: str,
    app_properties: dict[str, str],
) -> dict:
    """
    폴더에 파일을 올린다.

    Args:
        folder_id: 올릴 폴더 ID
        name: 파일 이름
        content: 파일 바이트
        mime_type: 파일 MIME 타입
        app_properties: 파일에 붙일 앱 전용 속성 (사람에게는 보이지 않는다)

    Returns:
        dict: 생성된 파일의 id·name
    """
    metadata = {"name": name, "parents": [folder_id], "appProperties": app_properties}
    # Drive 는 multipart/related 만 받는다. requests 의 files= 는 form-data 라 못 쓴다.
    body = (
        (
            f"--{MULTIPART_BOUNDARY}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{MULTIPART_BOUNDARY}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{MULTIPART_BOUNDARY}--".encode()
    )
    resp = _get_session().post(
        UPLOAD_URL,
        params={"uploadType": "multipart", "supportsAllDrives": True},
        headers={"Content-Type": f"multipart/related; boundary={MULTIPART_BOUNDARY}"},
        data=body,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()
