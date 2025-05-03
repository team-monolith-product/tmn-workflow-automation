from __future__ import annotations

import argparse
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Set, Tuple

from dotenv import load_dotenv
from slack_sdk import WebClient

from api.wantedspace import get_workevent, requests_get_with_retry
from service.slack import slack_call_with_retry

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

# ──────────────────────────── WantedSpace util ─────────────────────────────

def load_event_code_map() -> Dict[str, str]:
    """
    워크이벤트 코드와 텍스트의 매핑을 조회합니다.
    
    Returns:
        Dict[str, str]: 이벤트 코드와 이벤트 텍스트의 매핑 (예: {"WNS_VACATION_PM": "연차(오후)"})
    """
    url = f"{API_BASE}/workevent/event_codes/"
    resp = requests_get_with_retry(url, params=COMMON_QS, headers=HEADERS)
    resp.raise_for_status()
    return {item["code"]: item["text"] for item in resp.json()}


def fetch_absence_between(start_dt: datetime, end_dt: datetime) -> List[dict]:
    """
    특정 기간 내의 확정된 근태 이벤트를 조회합니다.
    
    Args:
        start_dt (datetime): 조회 시작 날짜
        end_dt (datetime): 조회 종료 날짜
        
    Returns:
        List[dict]: 근태 이벤트 목록 (상태가 "APPROVED" 또는 "INFORMED"인 이벤트만)
    """
    params = {
        **COMMON_QS,
        "type": "range",
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": end_dt.strftime("%Y-%m-%d"),
    }
    url = f"{API_BASE}/workevent/"
    events: List[dict] = []
    
    try:
        while url:
            r = requests_get_with_retry(url, params=params, headers=HEADERS)
            r.raise_for_status()
            js = r.json()
            events.extend(js.get("results", []))
            url = js.get("next")
            params = None  # next URL에 쿼리 포함
    except Exception as e:
        print(f"[ERROR] 근태 이벤트 조회 실패: {e}")
        return []
        
    return [ev for ev in events if ev.get("status") in {"APPROVED", "INFORMED"}]


def build_absence_set(
    events: List[dict],
    code_map: Dict[str, str],
    *,
    start: date,
    end: date,
) -> Set[Tuple[date, str, str]]:
    """
    근태 이벤트 목록에서 (날짜, 이름, 종류) 집합을 생성합니다.
    
    Args:
        events (List[dict]): 근태 이벤트 목록
        code_map (Dict[str, str]): 이벤트 코드와 텍스트 매핑
        start (date): 시작 날짜
        end (date): 종료 날짜
        
    Returns:
        Set[Tuple[date, str, str]]: (날짜, 이름, 종류) 집합
    """
    uniq: Set[Tuple[date, str, str]] = set()
    
    try:
        for ev in events:
            name = ev.get("username") or ev.get("email")
            kind = code_map.get(ev.get("wk_event")) or ev.get("event_name") or "기타"
            s = datetime.strptime(ev["wk_start_date"], "%Y-%m-%d").date()
            e = datetime.strptime(ev["wk_end_date"], "%Y-%m-%d").date()
            cur = max(s, start)
            while cur <= e and cur <= end:
                uniq.add((cur, name, kind))
                cur += timedelta(days=1)
    except Exception as e:
        print(f"[ERROR] 근태 데이터 처리 실패: {e}")
        
    return uniq

# ──────────────────────────── 한글 날짜 포맷터 ─────────────────────────────
KOREAN_WEEKDAY = "월화수목금토일"  # Monday=0 → "월"

def fmt(dt: date) -> str:
    """
    날짜를 한글 포맷으로 변환합니다.
    
    Args:
        dt (date): 변환할 날짜
        
    Returns:
        str: 변환된 문자열 (예: "5월 3일(금)")
    """
    return f"{dt.month}월 {dt.day}일({KOREAN_WEEKDAY[dt.weekday()]})"

# ────────────────────────────── 요약 생성 로직 ──────────────────────────────

def _compress_person_ranges(dates: List[date], kind_by_date: Dict[date, str]) -> List[Tuple[date, date, str]]:
    """
    단일 인원의 연속된 근태 구간을 압축합니다.
    
    Args:
        dates (List[date]): 날짜 목록
        kind_by_date (Dict[date, str]): 날짜별 근태 종류
        
    Returns:
        List[Tuple[date, date, str]]: (시작일, 종료일, 종류) 목록
    """
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
    """
    근태 데이터를 이름·종류별로 한 줄씩 정리한 요약을 생성합니다.
    
    Args:
        absence_set (Set[Tuple[date, str, str]]): (날짜, 이름, 종류) 집합
        
    Returns:
        str: 요약 문자열
    """
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
    """
    메인 함수: 명령행 인자를 파싱하고 예정된 근태 정보를 수집하여
    요약을 생성한 후 Slack 채널에 전송합니다.
    
    명령행 옵션:
    --days: 조회할 미래 일수 (기본값: 10일)
    --dry-run: 실제 메시지 전송 없이 콘솔에만 출력
    """
    p = argparse.ArgumentParser(description="팀 근태 요약 봇 (인원별)")
    p.add_argument("--days", type=int, default=DEFAULT_LOOKAHEAD_DAYS, help="조회 일수")
    p.add_argument("--dry-run", action="store_true", help="콘솔 출력만")
    args = p.parse_args()

    today = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = today + timedelta(days=args.days - 1)

    try:
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
        print("Slack 메시지 전송 완료.")
    except Exception as e:
        print(f"[ERROR] 실행 중 오류 발생: {e}")


if __name__ == "__main__":
    main()