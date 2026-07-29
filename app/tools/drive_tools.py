"""
Google Drive 관련 LangChain Tools

작업 공간(루트 폴더)이 주어지면 검색·읽기·쓰기가 모두 그 하위 트리로 제한된다.
제한은 프롬프트가 아니라 코드에서 걸므로 모델이 어긋날 수 없다.
"""

import asyncio

import fitz
from googleapiclient.errors import HttpError
from langchain_core.tools import tool
from typing import Annotated

from api import google_drive

from . import drive_scope

# Google 네이티브 문서를 텍스트로 읽을 때 사용할 변환 MIME 타입
EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/markdown",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# 별도 변환 없이 그대로 디코딩할 수 있는 MIME 타입
PLAIN_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
}

# 한 파일에서 읽어 들일 최대 문자 수 (에이전트 컨텍스트 보호)
MAX_CONTENT_CHARS = 40000

GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"

OUT_OF_SCOPE = (
    "이 파일은 지금 채널의 작업 공간 밖에 있어 접근할 수 없습니다. "
    "채널 캔버스에 적힌 폴더 하위만 다룰 수 있습니다."
)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _extract_pdf_text(data: bytes) -> str:
    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n\n".join(page.get_text() for page in doc)


def _truncate(text: str) -> str:
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    return (
        text[:MAX_CONTENT_CHARS] + f"\n\n[...{len(text) - MAX_CONTENT_CHARS}자 생략됨. "
        "전체가 필요하면 사용자에게 범위를 좁혀 달라고 요청하세요.]"
    )


def _read_file_content(file_id: str, root_folder_id: str | None) -> str:
    """파일 유형에 맞는 방식으로 본문 텍스트를 추출한다."""
    metadata = google_drive.get_file_metadata(file_id)

    if root_folder_id:
        folder_ids = drive_scope.folder_scope(root_folder_id)
        if not drive_scope.is_within_scope(metadata.get("parents"), folder_ids):
            return OUT_OF_SCOPE

    mime_type = metadata["mimeType"]
    name = metadata.get("name", "")

    if mime_type in EXPORT_MIME_TYPES:
        text = _decode(google_drive.export_file(file_id, EXPORT_MIME_TYPES[mime_type]))
    elif mime_type == "application/pdf":
        text = _extract_pdf_text(google_drive.download_file(file_id))
    elif mime_type.startswith("text/") or mime_type in PLAIN_TEXT_MIME_TYPES:
        text = _decode(google_drive.download_file(file_id))
    else:
        return (
            f"'{name}'은(는) 텍스트로 읽을 수 없는 형식입니다 (mimeType: {mime_type}). "
            "지원 형식: Google 문서/스프레드시트/프레젠테이션, PDF, 텍스트 계열."
        )

    return f"# {name}\n\n{_truncate(text)}"


def _scoped_query(query: str, root_folder_id: str | None) -> str:
    """검색 쿼리를 작업 공간 하위로 묶는다."""
    if not root_folder_id:
        return query

    folder_ids = drive_scope.folder_scope(root_folder_id)
    return f"{drive_scope.scope_clause(folder_ids)} and ({query})"


def _search(query: str, page_size: int, root_folder_id: str | None) -> dict:
    return google_drive.list_files(_scoped_query(query, root_folder_id), page_size)


def _write(
    name: str,
    content: str,
    file_id: str | None,
    folder_id: str | None,
    as_google_doc: bool,
    root_folder_id: str | None,
) -> str:
    folder_ids = drive_scope.folder_scope(root_folder_id) if root_folder_id else []

    if file_id:
        if root_folder_id:
            metadata = google_drive.get_file_metadata(file_id)
            if not drive_scope.is_within_scope(metadata.get("parents"), folder_ids):
                return OUT_OF_SCOPE

        result = google_drive.update_file(file_id, content, "text/markdown")
        return f"'{result['name']}'을(를) 덮어썼습니다. {result.get('webViewLink', '')}"

    target_folder_id = folder_id or root_folder_id
    if not target_folder_id:
        return (
            "새 파일을 만들 폴더를 알 수 없습니다. "
            "search_drive_files로 대상 폴더를 찾아 folder_id를 지정하세요."
        )

    if root_folder_id and target_folder_id not in folder_ids:
        return OUT_OF_SCOPE

    result = google_drive.create_file(
        name,
        target_folder_id,
        content,
        "text/markdown",
        GOOGLE_DOC_MIME_TYPE if as_google_doc else None,
    )
    return f"'{result['name']}'을(를) 생성했습니다. {result.get('webViewLink', '')}"


def get_drive_tools(root_folder_id: str | None) -> list:
    """
    Drive 도구 세 개를 반환한다.

    Args:
        root_folder_id: 작업 공간 루트 폴더. 지정하면 검색·읽기·쓰기가 모두
            그 하위 트리로 제한된다. None이면 접근 가능한 Drive 전체를 다룬다.
    """

    @tool
    async def search_drive_files(
        query: Annotated[
            str,
            "Drive API 검색 쿼리(q 파라미터). "
            "예: \"name contains '기획'\", "
            "\"fullText contains '연수'\", "
            "\"mimeType = 'application/vnd.google-apps.folder'\"",
        ],
        page_size: Annotated[int, "최대 결과 수"] = 20,
    ) -> str:
        """
        Google Drive에서 파일과 폴더를 검색합니다.

        파일을 읽거나 수정하기 전에 이 도구로 먼저 file_id를 찾아야 합니다.
        검색 범위는 이 채널의 작업 공간 하위로 자동 제한되므로,
        폴더 조건을 직접 넣을 필요가 없습니다.

        자주 쓰는 쿼리 패턴:
        - 이름으로 찾기: name contains '검색어'
        - 폴더 안의 항목: '폴더ID' in parents
        - 내용으로 찾기: fullText contains '검색어'
        - 폴더만: mimeType = 'application/vnd.google-apps.folder'
        - 휴지통 제외(권장): 위 조건에 and trashed = false 를 덧붙임

        Returns:
            str: 검색 결과 목록 (이름, ID, 형식, 수정일시)
        """
        try:
            response = await asyncio.to_thread(
                _search, query, page_size, root_folder_id
            )
        except HttpError as e:
            return f"Drive 검색 실패: {e}. 쿼리 문법을 확인하고 다시 시도하세요."

        files = response.get("files", [])
        if not files:
            return "검색 결과가 없습니다. 다른 검색어나 조건으로 다시 시도해 보세요."

        return "\n".join(
            f"- {f['name']} | id={f['id']} | {f['mimeType']} "
            f"| 수정 {f.get('modifiedTime', '?')}"
            for f in files
        )

    @tool
    async def read_drive_file(
        file_id: Annotated[str, "search_drive_files로 찾은 파일 ID"],
    ) -> str:
        """
        Google Drive 파일의 내용을 텍스트로 읽습니다.

        Google 문서는 마크다운, 스프레드시트는 CSV, 프레젠테이션은 텍스트로 변환하여 읽습니다.
        PDF는 텍스트를 추출합니다. 이미지 등 그 외 형식은 읽을 수 없습니다.

        추측하지 말고, 답변에 필요한 파일은 반드시 이 도구로 직접 읽어서 근거를 확인하세요.

        Returns:
            str: 파일 이름과 본문 텍스트
        """
        try:
            return await asyncio.to_thread(_read_file_content, file_id, root_folder_id)
        except HttpError as e:
            return f"파일 읽기 실패 (file_id={file_id}): {e}"

    @tool
    async def write_drive_file(
        name: Annotated[str, "파일 이름"],
        content: Annotated[
            str, "파일 본문. 마크다운 문법을 사용하면 서식이 반영됩니다."
        ],
        file_id: Annotated[
            str | None,
            "지정하면 해당 파일의 본문을 덮어씁니다. 새로 만들 때는 생략합니다.",
        ] = None,
        folder_id: Annotated[
            str | None,
            "새 파일을 만들 상위 폴더 ID. 생략하면 이 채널 작업 공간의 루트에 만듭니다.",
        ] = None,
        as_google_doc: Annotated[
            bool, "True면 Google 문서로, False면 마크다운 텍스트 파일로 저장합니다."
        ] = True,
    ) -> str:
        """
        Google Drive에 파일을 생성하거나 기존 파일을 덮어씁니다.

        file_id를 지정하면 덮어쓰기, 지정하지 않으면 새로 생성합니다.
        덮어쓰기는 기존 내용을 완전히 대체하므로, 일부만 고칠 때는 먼저 read_drive_file로
        전체를 읽어 수정된 전체 본문을 전달하세요.

        이 채널 작업 공간 하위에만 쓸 수 있습니다.

        Returns:
            str: 저장된 파일의 이름과 링크
        """
        try:
            return await asyncio.to_thread(
                _write,
                name,
                content,
                file_id,
                folder_id,
                as_google_doc,
                root_folder_id,
            )
        except HttpError as e:
            return (
                f"파일 저장 실패: {e}. "
                "공유 드라이브 하위 폴더인지, 서비스 계정에 쓰기 권한이 있는지 "
                "확인이 필요합니다."
            )

    return [search_drive_files, read_drive_file, write_drive_file]
