
from __future__ import annotations

import argparse
import functools
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Set, Tuple

import requests
from dotenv import load_dotenv
from slack_sdk import WebClient

# ────────────────────────────── 환경변수 & 상수 ──────────────────────────────
load_dotenv()

SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN")
WANTEDSPACE_API_KEY = os.getenv("WANTEDSPACE_API_KEY")
WANTEDSPACE_API_SECRET = os.getenv("WANTEDSPACE_API_SECRET")

if not all([SLACK_TOKEN, WANTEDSPACE_API_KEY, WANTEDSPACE_API_SECRET]):
    raise RuntimeError(
        "SLACK_BOT_TOKEN, WANTEDSPACE_API_KEY, WANTEDSPACE_API_SECRET 가 모두 필요합니다.")

CHANNEL_ID = "C08EUJJSZF1"  # 게시 채널
DEFAULT_LOOKAHEAD_DAYS = 10
API_BASE = "https://api.wantedspace.ai/tools/openapi"
HEADERS = {"Authorization": WANTEDSPACE_API_SECRET}
COMMON_QS = {"key": WANTEDSPACE_API_KEY}

# ──────────────────────────────── Slack util ────────────────────────────────

def slack_call_with_retry(method, max_retries: int = 3, **kwargs):
    """Slack API 429(Rate Limit) 대응 지수 백오프 재시도"""
    from slack_sdk.errors import SlackApiError

    backoff = 4
    for attempt in range(1, max_retries + 1):
        try:
            return method(**kwargs)
        except SlackApiError as e:
            if e.response.status_code == 429 or "rate_limited" in str(e):
                if attempt == max_retries:
                    raise
                time.sleep(backoff)
                backoff *= 2
            else:
                raise

# ───────────────────────────── WantedSpace util ─────────────────────────────

@functools.lru_cache(maxsize=1)
def load_event_code_map() -> Dict[str, str]:
    """/workevent/event_codes/ → {code: text}"""
    url = f"{API_BASE}/workevent/event_codes/"
    resp = requests.get(url, headers=HEADERS, params=COMMON_QS, timeout=10)
    resp.raise_for_status()
    return {item["code"]: item["text"] for item in resp.json()}


def fetch_absence_between(start_dt: datetime, end_dt: datetime) -> List[dict]:
    """`type=range`로 start_dt~end_dt 확정 근태 이벤트 조회"""
    params = {
        **COMMON_QS,
        "type": "range",
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": end_dt.strftime("%Y-%m-%d"),
    }
    url = f"{API_BASE}/workevent/"
    events: List[dict] = []
    while url:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        js = r.json()
        events.extend(js.get("results", []))
        url = js.get("next")
        params = None  # next URL에 쿼리 포함
    return [ev for ev in events if ev.get("status") in {"APPROVED", "INFORMED"}]


def build_absence_set(
    events: List[dict],
    code_map: Dict[str, str],
    *,
    start: date,
    end: date,
) -> Set[Tuple[date, str, str]]:
    """(date, name, kind) 집합 생성 (기간 필터 포함)"""
    uniq: Set[Tuple[date, str, str]] = set()
    for ev in events:
        name = ev.get("username") or ev.get("email")
        kind = code_map.get(ev.get("wk_event")) or ev.get("event_name") or "기타"
        s = datetime.strptime(ev["wk_start_date"], "%Y-%m-%d").date()
        e = datetime.strptime(ev["wk_end_date"], "%Y-%m-%d").date()
        cur = max(s, start)
        while cur <= e and cur <= end:
            uniq.add((cur, name, kind))
            cur += timedelta(days=1)
    return uniq

# ──────────────────────────── 한글 날짜 포맷터 ─────────────────────────────
KOREAN_WEEKDAY = "월화수목금토일"  # Monday=0 → "월"

def fmt(dt: date) -> str:
    return f"{dt.month}월 {dt.day}일({KOREAN_WEEKDAY[dt.weekday()]})"

# ────────────────────────────── 요약 생성 로직 ──────────────────────────────

def _compress_person_ranges(dates: List[date], kind_by_date: Dict[date, str]) -> List[Tuple[date, date, str]]:
    """단일 인원의 연속 구간 압축"""
    if not dates:
        return []
    ranges: List[Tuple[date, date, str]] = []
    start = last = dates[0]
    cur_kind = kind_by_date[start]
    for d in dates[1:]:
        k = kind_by_date[d]
        if k == cur_kind and (d - last).days == 1:
            last = d
        else:
            ranges.append((start, last, cur_kind))
            start = last = d
            cur_kind = k
    ranges.append((start, last, cur_kind))
    return ranges


def make_summary(absence_set: Set[Tuple[date, str, str]]) -> str:
    """이름·종류별로 한 줄씩 정리한 줄글 요약"""
    if not absence_set:
        return "예정된 근태 이벤트가 없습니다."

    # 이름→{date:kind}
    person_map: defaultdict[str, Dict[date, str]] = defaultdict(dict)
    for dt, name, kind in absence_set:
        person_map[name][dt] = kind

    lines: List[str] = []
    for name in sorted(person_map):
        kd = person_map[name]
        ranges = _compress_person_ranges(sorted(kd), kd)
        for s, e, kind in ranges:
            if s == e:
                lines.append(f"{name} : {fmt(s)} {kind}")
            else:
                lines.append(f"{name} : {fmt(s)}-{fmt(e)} {kind}")
    return "\n".join(lines)

# ──────────────────────────────── 메인 진입 ─────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="팀 근태 요약 봇 (인원별)")
    p.add_argument("--days", type=int, default=DEFAULT_LOOKAHEAD_DAYS, help="조회 일수")
    p.add_argument("--dry-run", action="store_true", help="콘솔 출력만")
    args = p.parse_args()

    today = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = today + timedelta(days=args.days - 1)

    events = fetch_absence_between(today, end_dt)
    code_map = load_event_code_map()
    absence = build_absence_set(events, code_map, start=today.date(), end=end_dt.date())

    summary = make_summary(absence)
    title = f"앞으로 {args.days}일간 예정 근태 현황"

    if args.dry_run:
        print("==== DRY RUN ====")
        print(title)
        print(summary)
        return

    slack = WebClient(token=SLACK_TOKEN)
    slack_call_with_retry(
        slack.chat_postMessage,
        channel=CHANNEL_ID,
        text=title,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": summary}}],
    )


if __name__ == "__main__":
    main()
