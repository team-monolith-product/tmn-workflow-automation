"""
S0 수집 — source_registry 의 활성 소스에서 공고를 모은다.

현재 어댑터: g2b (나라장터). 새 소스는 어댑터 분기를 추가하고
source_registry 에 enabled 로 등록하면 된다.
"""

import time

import requests

from api.g2b import get_bid_pblanc_list, KIND_LABELS
from .schemas import Announcement
from .stages import to_announcement

_PAGE_SIZE = 100
_MAX_PAGES = 50
_FETCH_RETRIES = 4
_FETCH_RETRY_WAIT = 2.0


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


def _fetch_page(kind: str, bgn: str, end: str, page: int, session) -> dict:
    """게이트웨이 일시 오류(403/5xx)는 짧게 재시도."""
    last_exc = None
    for attempt in range(_FETCH_RETRIES):
        try:
            return get_bid_pblanc_list(
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


def _collect_g2b(source: dict, bgn: str, end: str, session) -> list[Announcement]:
    kind = source["kind"]
    source_id = source["id"]
    out: list[Announcement] = []
    page = 1
    while page <= _MAX_PAGES:
        payload = _fetch_page(kind, bgn, end, page, session)
        items, total = _extract_items(payload)
        for it in items:
            out.append(to_announcement(it, source_id, kind, KIND_LABELS[kind]))
        if not items or len(out) >= total:
            break
        page += 1
    return out


def collect(knowledge, window: tuple[str, str], session=None) -> list[Announcement]:
    """활성 소스 전부에서 공고를 수집한다."""
    bgn, end = window
    collected: list[Announcement] = []
    for source in knowledge.enabled_sources:
        adapter = source.get("adapter")
        if adapter == "g2b":
            collected.extend(_collect_g2b(source, bgn, end, session))
        else:
            print(
                f"[edu-bid] 미지원 어댑터 '{adapter}' (source={source.get('id')}) 건너뜀"
            )
    return collected
