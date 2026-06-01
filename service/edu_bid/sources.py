"""
S0 수집 — source_registry 의 활성 소스에서 공고를 모은다.

현재 어댑터: g2b / g2b_presearch (나라장터). 새 소스는 _ADAPTERS 에 한 줄
추가하고 source_registry 에 enabled 로 등록하면 된다.
"""

import json
import time
from pathlib import Path

import requests

from api.g2b import get_bid_pblanc_list, get_pre_spec_list, KIND_LABELS
from .schemas import Announcement
from .stages import to_announcement, to_announcement_prespec

_PAGE_SIZE = 100
_MAX_PAGES = 50
_FETCH_RETRIES = 4
_FETCH_RETRY_WAIT = 2.0

# 날짜·소스 단위 원본 캐시 (조회 구간은 지나간 하루라 불변 → TTL 불필요)
_CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "edu_bid"


def _cache_path(source_id: str, bgn: str, end: str) -> Path:
    return _CACHE_DIR / f"{source_id}_{bgn}_{end}.json"


def _extract_items(payload: dict) -> tuple[list[dict], int]:
    response = payload.get("response", {})
    header = response.get("header", {})
    result_code = header.get("resultCode")
    if result_code not in (None, "00", "INFO-0", "0"):
        msg = header.get("resultMsg", "알 수 없는 오류")
        raise RuntimeError(f"G2B API 오류 (resultCode={result_code}): {msg}")
    body = response.get("body", {})
    total = int(body.get("totalCount", 0) or 0)
    items = body.get("items", [])
    if items in ("", None):
        return [], total
    if isinstance(items, dict):
        inner = items.get("item", items)
        if isinstance(inner, dict):
            return [inner], total
        if isinstance(inner, list):
            return inner, total
        return [], total
    if isinstance(items, list):
        return items, total
    return [], total


# 어댑터 id → (목록 조회 함수, 정규화 함수, 업무구분 라벨 접미)
_ADAPTERS = {
    "g2b": (get_bid_pblanc_list, to_announcement, ""),
    "g2b_presearch": (get_pre_spec_list, to_announcement_prespec, "(사전규격)"),
}


def _fetch_page(fetch_fn, kind: str, bgn: str, end: str, page: int, session) -> dict:
    """한 페이지 조회. 게이트웨이 일시 오류(403/5xx)는 짧게 재시도."""
    last_exc = None
    for attempt in range(_FETCH_RETRIES):
        try:
            return fetch_fn(
                kind, bgn, end, page_no=page, num_of_rows=_PAGE_SIZE, session=session
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status != 403 and not (status and 500 <= status < 600):
                raise
            last_exc = exc
            print(
                f"[edu-bid] {kind} p{page} 일시오류 {status} 재시도 {attempt + 1}/{_FETCH_RETRIES}"
            )
            time.sleep(_FETCH_RETRY_WAIT)
    raise last_exc


def _paginate(fetch_fn, kind: str, bgn: str, end: str, session) -> list[dict]:
    """구간 전체를 페이지네이션으로 수집(페이지별 403/5xx 재시도 포함)."""
    out: list[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        payload = _fetch_page(fetch_fn, kind, bgn, end, page, session)
        items, total = _extract_items(payload)
        out.extend(items)
        if not items or len(out) >= total:
            break
        page += 1
    return out


def _collect_adapter(
    source: dict, bgn: str, end: str, session, use_cache: bool
) -> list[Announcement]:
    kind = source["kind"]
    source_id = source["id"]
    fetch_fn, normalize, label_suffix = _ADAPTERS[source["adapter"]]
    cache = _cache_path(source_id, bgn, end)

    if use_cache and cache.exists():
        raw_items = json.loads(cache.read_text(encoding="utf-8"))
        print(
            f"[edu-bid] 캐시 사용 {source_id} {bgn}~{end}: {len(raw_items)}건 (API 미호출)"
        )
    else:
        raw_items = _paginate(fetch_fn, kind, bgn, end, session)
        if use_cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps(raw_items, ensure_ascii=False), encoding="utf-8"
            )
            print(f"[edu-bid] 캐시 저장 {source_id} {bgn}~{end}: {len(raw_items)}건")

    label = KIND_LABELS[kind] + label_suffix
    return [normalize(it, source_id, kind, label) for it in raw_items]


def collect(
    knowledge, window: tuple[str, str], session=None, use_cache: bool = True
) -> list[Announcement]:
    """활성 소스 전부에서 공고를 수집한다. 날짜·소스 단위 원본 캐시 사용."""
    bgn, end = window
    collected: list[Announcement] = []
    for source in knowledge.enabled_sources:
        adapter = source.get("adapter")
        if adapter in _ADAPTERS:
            collected.extend(_collect_adapter(source, bgn, end, session, use_cache))
        else:
            print(
                f"[edu-bid] 미지원 어댑터 '{adapter}' (source={source.get('id')}) 건너뜀"
            )
    return collected
