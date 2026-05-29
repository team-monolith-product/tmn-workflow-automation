"""
나라장터(G2B) 교육 외주 입찰공고 수집·평가

매일 직전 구간(기본 1일)에 게시된 입찰공고를 업무구분별로 수집하고,
각 공고를 LLM으로 전수 평가하여 우리 교육 외주 사업영역 적합도(0~100)를 매긴 뒤,
임계 점수 이상인 공고만 Slack 채널에 보고한다.

중복 방지: 매 실행이 "직전 N일"이라는 서로 겹치지 않는 게시일 구간만 조회하므로
별도의 상태 저장 없이 같은 공고를 다시 보고하지 않는다.

독립 실행:
    python scripts/crawl_education_bids.py            # 실제 Slack 전송
    python scripts/crawl_education_bids.py --dry-run  # 전송 없이 콘솔 출력
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import argparse
from datetime import date, datetime, time, timedelta

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from slack_sdk import WebClient

from api.g2b import get_bid_pblanc_list, KIND_LABELS
from service.config import load_config, EducationBidCrawlerConfig

load_dotenv()

_PAGE_SIZE = 100
_MAX_PAGES = 50  # 무한 페이지네이션 방지


# --- LLM 평가 스키마 ---


class BidEvaluation(BaseModel):
    """공고 1건에 대한 적합도 평가"""

    index: int = Field(description="입력으로 준 공고의 번호(index)")
    score: int = Field(description="우리 사업영역 적합도 0~100", ge=0, le=100)
    category: str = Field(
        description="적합 영역 분류(예: 플랫폼개발, 콘텐츠제작, 교육운영). 부적합이면 '해당없음'"
    )
    reason: str = Field(description="점수 근거 한 줄")


class BatchEvaluation(BaseModel):
    """배치 평가 결과"""

    evaluations: list[BidEvaluation]


# --- 조회 구간 ---


def build_window(today: date, lookback_days: int) -> tuple[str, str]:
    """게시일 조회 구간을 YYYYMMDDHHMM 문자열로 만든다.

    [today - lookback_days, today) 구간(어제 00:00 ~ 오늘 00:00 직전).
    일 1회 실행 시 구간이 달력일 단위로 정확히 타일링되어 중복이 없다.
    """
    end_dt = datetime.combine(today, time.min)
    bgn_dt = end_dt - timedelta(days=lookback_days)
    end_dt = end_dt - timedelta(minutes=1)  # 오늘 00:00 직전(어제 23:59)
    fmt = "%Y%m%d%H%M"
    return bgn_dt.strftime(fmt), end_dt.strftime(fmt)


# --- 수집/정규화 ---


def extract_items(payload: dict) -> tuple[list[dict], int]:
    """G2B 응답 dict에서 (items, totalCount)를 추출·정규화한다.

    결과 0건이면 items가 ""/None, 1건이면 dict, 다건이면 list로 오는 등
    형태가 불안정하므로 정규화한다.
    """
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


def collect_announcements(
    kinds: list[str], inqry_bgn: str, inqry_end: str, session=None
) -> list[dict]:
    """업무구분별로 구간 내 전체 공고를 페이지네이션하며 수집한다."""
    collected: list[dict] = []
    for kind in kinds:
        page = 1
        while page <= _MAX_PAGES:
            payload = get_bid_pblanc_list(
                kind,
                inqry_bgn,
                inqry_end,
                page_no=page,
                num_of_rows=_PAGE_SIZE,
                session=session,
            )
            items, total = extract_items(payload)
            for item in items:
                item["_kind"] = kind
                item["_kind_label"] = KIND_LABELS[kind]
            collected.extend(items)
            if not items or sum(1 for c in collected if c["_kind"] == kind) >= total:
                break
            page += 1
    return collected


def _first(item: dict, *keys: str) -> str:
    """여러 후보 필드명 중 처음으로 값이 있는 것을 반환(스키마 변형 대응)."""
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def summarize_item(item: dict) -> dict:
    """공고 item에서 보고·평가에 필요한 핵심 필드만 뽑는다."""
    return {
        "bid_no": _first(item, "bidNtceNo", "bidno"),
        "bid_ord": _first(item, "bidNtceOrd"),
        "title": _first(item, "bidNtceNm"),
        "notice_inst": _first(item, "ntceInsttNm"),
        "demand_inst": _first(item, "dminsttNm"),
        "notice_dt": _first(item, "bidNtceDt", "bidNtceDate"),
        "close_dt": _first(item, "bidClseDt", "bidClseDate"),
        "opening_dt": _first(item, "opengDt", "opengDate"),
        "estimated_price": _first(item, "presmptPrce", "asignBdgtAmt"),
        "url": _first(item, "bidNtceDtlUrl", "bidNtceUrl"),
        "kind_label": item.get("_kind_label", ""),
    }


# --- LLM 전수 평가 ---


def _build_eval_prompt(profile: str, summaries: list[dict]) -> str:
    lines = [
        "당신은 교육 분야 외주(B2G/공공조달) 사업개발 담당자입니다.",
        "아래 우리 회사 프로필을 기준으로, 각 입찰공고가 우리가 수주를 노릴 만한지 0~100점으로 평가하세요.",
        "사업 도메인이 교육/이러닝/에듀테크/SW교육/콘텐츠와 무관하면 낮은 점수를 주세요.",
        "",
        "# 우리 회사 프로필",
        profile.strip(),
        "",
        "# 평가 대상 공고",
    ]
    for i, s in enumerate(summaries):
        price = s["estimated_price"] or "미상"
        lines.append(
            f"[{i}] ({s['kind_label']}) {s['title']} | 수요기관: {s['demand_inst'] or s['notice_inst'] or '미상'} | 추정가격: {price}"
        )
    lines.append("")
    lines.append(
        "각 공고마다 index, score(0~100), category(적합 영역 또는 '해당없음'), reason(한 줄)을 평가하세요."
    )
    return "\n".join(lines)


def evaluate_announcements(
    summaries: list[dict], profile: str, model: str, batch_size: int, llm=None
) -> dict[int, BidEvaluation]:
    """공고 요약 리스트를 배치로 전수 평가하여 {index: BidEvaluation}을 반환한다."""
    if not summaries:
        return {}
    client = llm or ChatOpenAI(model=model, temperature=0).with_structured_output(
        BatchEvaluation
    )

    results: dict[int, BidEvaluation] = {}
    for start in range(0, len(summaries), batch_size):
        batch = summaries[start : start + batch_size]
        prompt = _build_eval_prompt(profile, batch)
        out: BatchEvaluation = client.invoke(prompt)
        for ev in out.evaluations:
            global_idx = start + ev.index
            if 0 <= ev.index < len(batch):
                results[global_idx] = ev
    return results


# --- Slack 보고 ---


def format_slack_text(
    qualified: list[tuple[dict, BidEvaluation]], window: tuple[str, str]
) -> str:
    """적합 공고를 점수 내림차순으로 Slack 메시지 텍스트로 만든다."""
    bgn, end = window
    bgn_disp = f"{bgn[:4]}-{bgn[4:6]}-{bgn[6:8]}"
    header = (
        f":mega: 교육 외주 적합 입찰공고 {len(qualified)}건 (게시일 {bgn_disp} 구간)"
    )
    blocks = [header, ""]
    for s, ev in qualified:
        price = (
            f"{int(s['estimated_price']):,}원"
            if s["estimated_price"].isdigit()
            else (s["estimated_price"] or "미상")
        )
        title_line = f"*[{ev.score}점·{ev.category}] {s['title']}* ({s['kind_label']})"
        meta = f" · 수요기관: {s['demand_inst'] or s['notice_inst'] or '미상'} | 추정가격: {price} | 마감: {s['close_dt'] or '미상'}"
        reason = f" · 근거: {ev.reason}"
        url = f" · {s['url']}" if s["url"] else ""
        blocks.append(title_line)
        blocks.append(meta)
        blocks.append(reason)
        if url:
            blocks.append(url)
        blocks.append("")
    return "\n".join(blocks).rstrip()


# --- 엔트리포인트 ---


def run(cfg: EducationBidCrawlerConfig, today: date, dry_run: bool, session=None):
    window = build_window(today, cfg.lookback_days)
    print(f"[edu-bid] 조회 구간: {window[0]} ~ {window[1]} / kinds={cfg.kinds}")

    raw_items = collect_announcements(cfg.kinds, window[0], window[1], session=session)
    print(f"[edu-bid] 수집 공고: {len(raw_items)}건")
    if not raw_items:
        print("[edu-bid] 공고 없음. 종료.")
        return

    summaries = [summarize_item(it) for it in raw_items]
    evaluations = evaluate_announcements(
        summaries, cfg.business_profile, cfg.model, cfg.batch_size
    )

    qualified = [
        (summaries[i], ev)
        for i, ev in evaluations.items()
        if ev.score >= cfg.score_threshold
    ]
    qualified.sort(key=lambda pair: pair[1].score, reverse=True)
    print(f"[edu-bid] 적합({cfg.score_threshold}점 이상): {len(qualified)}건")

    if not qualified:
        print("[edu-bid] 적합 공고 없음. Slack 전송 생략.")
        return

    text = format_slack_text(qualified, window)
    if dry_run:
        print("----- DRY RUN: Slack 메시지 -----")
        print(text)
        return

    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    slack_client.chat_postMessage(channel=cfg.channel_id, text=text)
    print(f"[edu-bid] Slack 전송 완료 → {cfg.channel_id}")


def main():
    parser = argparse.ArgumentParser(
        description="나라장터 교육 외주 입찰공고 수집·평가"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Slack 전송 없이 콘솔 출력"
    )
    args = parser.parse_args()

    config = load_config()
    cfg = config.education_bid_crawler
    if not cfg:
        print("education_bid_crawler 설정이 config.yaml에 없습니다.")
        return

    run(cfg, date.today(), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
