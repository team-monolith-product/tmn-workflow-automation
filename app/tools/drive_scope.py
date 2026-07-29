"""
Drive 접근 범위를 폴더 하위 트리로 제한하는 유틸리티

채널 캔버스에 적힌 Drive 폴더를 그 채널의 작업 공간으로 삼는다.
프롬프트로 "이 폴더만 봐라"라고 지시하는 것과 달리, 여기서는 검색 쿼리와
파일 접근을 코드에서 막으므로 모델이 어긋날 수 없다.

Drive 검색의 `'X' in parents`는 직계 자식만 매칭한다. 재귀 검색이 없어서
하위 폴더 ID를 직접 모아야 한다.
"""

import re

from cachetools import TTLCache

from api import google_drive

# 폴더 URL. 마크다운 링크 안에 있어도 잡힌다.
_FOLDER_URL_RE = re.compile(
    r"drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)"
)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# 트리를 훑는 범위. 상한이 없으면 큰 드라이브에서 요청이 폭주한다.
MAX_SCOPE_FOLDERS = 200
MAX_SCOPE_DEPTH = 5

# 하위 트리는 자주 바뀌지 않는다. 대화 한 세션 동안은 한 번만 훑으면 된다.
_scope_cache = TTLCache(maxsize=32, ttl=600)


def extract_folder_id(text: str | None) -> str | None:
    """텍스트에서 첫 번째 Drive 폴더 ID를 뽑는다."""
    if not text:
        return None

    match = _FOLDER_URL_RE.search(text)
    return match.group(1) if match else None


def folder_scope(root_id: str) -> list[str]:
    """
    루트 폴더와 그 하위 폴더 ID를 모두 모은다.

    상한에 걸리면 거기서 멈춘다. 잘린 범위로 검색하면 일부를 못 찾을 뿐,
    범위 밖 파일이 새어 나오지는 않는다.
    """
    if root_id in _scope_cache:
        return _scope_cache[root_id]

    folder_ids = [root_id]
    frontier = [root_id]
    depth = 0

    while frontier and depth < MAX_SCOPE_DEPTH and len(folder_ids) < MAX_SCOPE_FOLDERS:
        next_frontier = []

        for parent in frontier:
            response = google_drive.list_files(
                f"'{parent}' in parents "
                f"and mimeType = '{FOLDER_MIME_TYPE}' and trashed = false",
                page_size=100,
            )
            for child in response.get("files", []):
                folder_ids.append(child["id"])
                next_frontier.append(child["id"])
                if len(folder_ids) >= MAX_SCOPE_FOLDERS:
                    break
            if len(folder_ids) >= MAX_SCOPE_FOLDERS:
                break

        frontier = next_frontier
        depth += 1

    _scope_cache[root_id] = folder_ids
    return folder_ids


def scope_clause(folder_ids: list[str]) -> str:
    """검색 쿼리에 AND로 덧붙일 범위 조건을 만든다."""
    return "(" + " or ".join(f"'{fid}' in parents" for fid in folder_ids) + ")"


def is_within_scope(parents: list[str] | None, folder_ids: list[str]) -> bool:
    """파일의 상위 폴더가 범위 안인지 확인한다."""
    if not parents:
        return False
    return any(parent in folder_ids for parent in parents)
