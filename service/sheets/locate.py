"""
"어느 시트냐" 를 정합니다.

사람은 URL 을 외우지 않습니다. "부산 만족도 시트" 라고 부릅니다. 그래서 링크가
오면 그대로 쓰고, 링크가 아니면 이름으로 찾습니다.

**후보가 여럿이면 고르지 않습니다.** 이름이 비슷한 시트가 실제로 여럿 있습니다 --
"…의 복사본", 기수별 사본, 정리용 사본. 하나를 골라 읽어 버리면 엉뚱한 명단으로
문자를 보내고, 문장이 자연스러워서 아무도 못 잡습니다.

링크 해석과 이름 필터가 여기 있습니다. api 는 목록을 그대로 주고, "사람이 부르는
말로 시트를 지목한다" 는 정책이라 이쪽이 주인입니다. 다만 **어느 탭이냐** 는
api 쪽입니다 -- 살아 있는 탭 목록이 있어야 풀리는 판단이라 그렇습니다.
"""

import re
import threading
import time
from typing import NamedTuple, TypedDict

from api.google_sheets import list_spreadsheet_files


class SheetFile(TypedDict, total=False):
    """Drive 가 돌려주는 스프레드시트 한 건."""

    id: str
    name: str
    modifiedTime: str
    webViewLink: str


# https://docs.google.com/spreadsheets/d/{id}/edit#gid={gid}
_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9-_]+)")
_GID = re.compile(r"[#&?]gid=([0-9]+)")
# 링크가 아니라 ID 만 붙여넣는 경우. 구글 시트 ID 는 43~44자이고 대소문자가 섞인다.
# 길이만 보면 "2026_customer_satisfaction_survey" 같은 영문 시트 **이름**이 ID 로
# 오인돼 검색을 건너뛰고, 사람은 "공유를 확인하십시오" 대신 구글 404 를 본다.
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{40,}$")


class Sheet(NamedTuple):
    """읽을 시트를 가리키는 값."""

    spreadsheet_id: str
    worksheet_id: int | None  # None 이면 첫 번째 탭


def parse_target(text: str) -> Sheet:
    """시트 링크나 ID 에서 스프레드시트와 탭을 뽑습니다.

    Args:
        text: 시트 URL 또는 스프레드시트 ID

    Returns:
        Sheet: worksheet_id 는 링크에 gid 가 없으면 None

    Raises:
        ValueError: 시트 링크로 읽을 수 없을 때
    """
    target = (text or "").strip()
    if not target:
        raise ValueError("시트 링크가 비어 있습니다.")
    if _BARE_ID.match(target):
        return Sheet(target, None)
    found = _ID.search(target)
    if not found:
        raise ValueError(f"시트 링크에서 ID 를 찾을 수 없습니다: {text}")
    gid = _GID.search(target)
    return Sheet(found.group(1), int(gid.group(1)) if gid else None)


class Found(NamedTuple):
    """찾은 결과. sheet 가 있으면 읽을 수 있고, 없으면 candidates 를 보여준다."""

    sheet: Sheet | None
    candidates: list[SheetFile]


def match_name(files: list[SheetFile], want: str) -> list[SheetFile]:
    """이름에 조각이 들어간 시트를 고릅니다. 대소문자는 무시합니다.

    Args:
        files: 스프레드시트 목록
        want: 찾을 이름 조각

    Returns:
        list[SheetFile]: 걸린 것. 입력 순서(최근 수정 순)를 유지한다
    """
    needle = want.strip().lower()
    return [item for item in files if needle in item.get("name", "").lower()]


# 드라이브 전량 목록은 94개에 1.1초다(8/21 실측). 에이전트가 시트 두셋을 이름으로
# 대조하면 그때마다 다시 나간다. 새 시트가 1분 늦게 보이는 것은 감수한다.
_TTL = 60.0
_cache: tuple[float, list[SheetFile]] | None = None
_cache_lock = threading.Lock()


def _files() -> list[SheetFile]:
    """볼 수 있는 스프레드시트 목록. 짧게 캐시합니다."""
    global _cache
    with _cache_lock:
        if _cache is None or time.monotonic() - _cache[0] > _TTL:
            _cache = (time.monotonic(), list_spreadsheet_files())
        return _cache[1]


def locate(target: str) -> Found:
    """링크면 그대로, 아니면 이름으로 찾습니다.

    Args:
        target: 시트 URL·ID 또는 이름 조각

    Returns:
        Found: 하나로 좁혀지면 sheet, 아니면 candidates

    Raises:
        ValueError: 이름으로도 못 찾았을 때
    """
    text = (target or "").strip()
    if not text:
        raise ValueError("어느 시트인지 알려주세요. 링크나 시트 이름이 필요합니다.")
    if _BARE_ID.match(text) or _ID.search(text):
        return Found(parse_target(text), [])
    if text.startswith("http") or "docs.google.com" in text:
        # 링크는 줬는데 시트 링크가 아닙니다. 구글 **문서**·프레젠테이션 링크가
        # 흔한데, 이것을 이름 검색으로 흘리면 "공유를 확인하십시오" 가 나가고
        # 사람은 공유 설정을 뒤집니다. 진짜 원인은 그게 시트가 아니라는 것입니다.
        raise ValueError(
            "시트 링크가 아닙니다. 구글 문서나 프레젠테이션 링크일 수 있습니다:"
            f" {text}"
        )

    hits = match_name(_files(), text)
    if not hits:
        raise ValueError(
            f"'{text}' 로 찾은 시트가 없습니다."
            " 이름 일부만 적어 보시고, 그래도 없으면 그 시트가 봇 서비스 계정에"
            " 공유되어 있는지 확인이 필요합니다."
        )
    if len(hits) == 1:
        return Found(Sheet(hits[0]["id"], None), hits)
    return Found(None, hits)


def render_candidates(candidates: list[SheetFile], limit: int = 10) -> str:
    """후보 목록을 사람이 고를 수 있게 적습니다.

    최근 수정 순으로 들어옵니다 -- 가장 최근에 손댄 사본이 보통 정답이라,
    앞에서 잘려도 고를 만한 것이 남습니다.

    Args:
        candidates: 스프레드시트 목록
        limit: 보여줄 개수

    Returns:
        str: 이름과 id 가 줄마다
    """
    lines = [f"{len(candidates)}개를 찾았습니다. 어느 것인지 알려주세요."]
    for item in candidates[:limit]:
        modified = (item.get("modifiedTime") or "")[:10]
        lines.append(f"· {item['name']}\t{item['id']}\t{modified}")
    if len(candidates) > limit:
        lines.append(f"… 외 {len(candidates) - limit}개 (최근 수정 순)")
    return "\n".join(lines)
