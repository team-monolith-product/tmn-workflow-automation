"""
해커톤 리드 아침 정리 (문의지기 F6 추후 연락일 알림 + F7 누락 자료 점검)

노션 「🗂 리드 파이프라인」을 읽어 오늘 연락할 리드와 자료가 빠진 리드를 한 글로
정리합니다. 아침 10시 DM(scripts/notify_hackathon_leads.py)과 로봇의 「점검」
도구(app/hackathon_inquiry.py)가 같은 함수를 씁니다.

노션 API 로는 파일을 첨부할 수 없으므로 빈 칸을 알려 주기만 합니다.
"""

import re
from dataclasses import dataclass
from datetime import date

from notion_client import Client as NotionClient
from notion_client.helpers import collect_paginated_api

from service.hackathon_consult import ESSENTIALS_DONE_CHECKBOX, ESSENTIALS_HEADING
from service.hackathon_consult import cell_text
from service.hackathon_inquiry import CHANNEL_ID, LEAD_DATA_SOURCE_ID

# 진행 순서. "X 이상" 은 이 목록에서 X 와 같거나 뒤.
STAGES = ("문의접수", "상담", "제안·견적", "협의", "수주·계약", "운영·완료")
OPEN_STAGES = ("문의접수", "상담", "제안·견적", "협의")
DROPPED_STAGES = ("무산", "보류")
STALE_DAYS = 14

SLACK_LINK_RE = re.compile(rf"/archives/{CHANNEL_ID}/p(\d{{10}})(\d{{6}})")
WEEKDAYS = "월화수목금토일"


@dataclass
class Lead:
    """리드 한 건에서 아침 정리에 쓰는 값"""

    page_id: str
    name: str
    status: str | None
    next_action: str
    next_action_date: date | None
    last_contact: date | None
    empty: set[str]
    slack_thread_ts: str | None


# 비었는지 보는 속성
CHECKED_PROPERTIES = (
    "📎 보낸 견적서",
    "📎 보낸 설명서·제안서",
    "견적규모(만원)",
    "수주확률",
    "희망일정",
    "확정금액(만원)",
    "🗓 행사일정(확정)",
    "📍 행사 장소",
    "무산 사유",
    "이탈 단계",
    "마지막 접촉일",
)


def _is_empty(prop: dict) -> bool:
    kind = prop["type"]
    value = prop[kind]
    if kind in ("rich_text", "title", "files"):
        return not value
    if kind in ("select", "date", "number"):
        return value is None
    raise ValueError(f"점검하지 않는 속성 종류입니다: {kind}")


def _date(prop: dict) -> date | None:
    value = prop["date"]
    return date.fromisoformat(value["start"][:10]) if value else None


def lead_from_page(page: dict) -> Lead:
    """노션 페이지를 Lead 로 읽습니다."""
    props = page["properties"]
    note = "".join(part["plain_text"] for part in props["비고"]["rich_text"])
    match = SLACK_LINK_RE.search(note)
    return Lead(
        page_id=page["id"],
        name="".join(part["plain_text"] for part in props["고객·기관명"]["title"])
        or "(기관명 없음)",
        status=(props["진행상태"]["select"] or {}).get("name"),
        next_action="".join(
            part["plain_text"] for part in props["다음 액션"]["rich_text"]
        ),
        next_action_date=_date(props["다음 액션일"]),
        last_contact=_date(props["마지막 접촉일"]),
        empty={name for name in CHECKED_PROPERTIES if _is_empty(props[name])},
        slack_thread_ts=f"{match.group(1)}.{match.group(2)}" if match else None,
    )


def _at_least(status: str | None, stage: str) -> bool:
    return status in STAGES and STAGES.index(status) >= STAGES.index(stage)


def needs_essentials_check(lead: Lead) -> bool:
    """상담 페이지 필수 7항목을 읽어 봐야 하는지 (상담 이상)"""
    return _at_least(lead.status, "상담")


def is_due(lead: Lead, today: date) -> bool:
    """오늘 연락할 리드인지 (F6)"""
    return (
        lead.status in OPEN_STAGES
        and lead.next_action_date is not None
        and lead.next_action_date <= today
    )


def missing_items(lead: Lead, essentials_filled: int | None, today: date) -> list[str]:
    """빠진 자료 (F7). 진행상태마다 있어야 할 것이 다릅니다.

    Args:
        lead: 리드
        essentials_filled: 필수 7항목 중 답이 있는 수. 읽지 않았으면 None
        today: 오늘

    Returns:
        list[str]: 빠진 것 한 마디씩
    """
    items: list[str] = []
    empty = lead.empty
    # 여러 단계 규칙에 같은 속성이 겹친다(보낸 견적서). 앞 규칙의 문구 하나만 남긴다.
    reported: set[str] = set()

    def need(name: str, label: str | None = None) -> None:
        if name not in empty or name in reported:
            return
        reported.add(name)
        items.append(
            label
            or f"{name.removeprefix('📎 ').removeprefix('🗓 ').removeprefix('📍 ')} 없음"
        )

    if lead.status in DROPPED_STAGES:
        need("무산 사유")
        need("이탈 단계")
        return items

    if _at_least(lead.status, "수주·계약"):
        need("📎 보낸 견적서", "보낸 견적서 없음 ← 계산서 발행 전 필요")
        need("확정금액(만원)")
        need("🗓 행사일정(확정)")
        need("📍 행사 장소")
    if _at_least(lead.status, "협의"):
        need("수주확률")
        need("희망일정")
    if _at_least(lead.status, "제안·견적"):
        need("📎 보낸 견적서")
        need("견적규모(만원)")
        need("📎 보낸 설명서·제안서")
    if _at_least(lead.status, "상담"):
        if essentials_filled is not None and essentials_filled < 7:
            items.append(f"필수 7항목 {essentials_filled}/7")
        need("마지막 접촉일")

    if (
        lead.status in OPEN_STAGES
        and lead.last_contact is not None
        and (today - lead.last_contact).days > STALE_DAYS
        and lead.next_action_date is None
    ):
        items.append(
            f"마지막 접촉 {(today - lead.last_contact).days}일 전, 다음 액션일 없음"
        )
    return items


def count_essentials(blocks: list[dict], rows: list[dict] | None) -> int | None:
    """필수 7항목 중 답이 있는 수. 「7항목 모두 확인」을 체크했으면 7.

    Args:
        blocks: 상담 페이지 최상위 블록들
        rows: 필수 7항목 표의 행들. 표가 없으면 None

    Returns:
        int | None: 템플릿 이전에 만든 페이지라 표가 없으면 None (점검하지 않음)
    """
    section = None
    for block in blocks:
        if block["type"] == "heading_2":
            section = "".join(
                p["plain_text"] for p in block["heading_2"]["rich_text"]
            ).strip()
        elif section == ESSENTIALS_HEADING and block["type"] == "to_do":
            text = "".join(p["plain_text"] for p in block["to_do"]["rich_text"])
            if ESSENTIALS_DONE_CHECKBOX in text and block["to_do"]["checked"]:
                return 7
    if rows is None:
        return None
    filled = 0
    for row in rows:
        cells = row["table_row"]["cells"]
        if cell_text(cells[0]).isdigit() and cell_text(cells[2]):
            filled += 1
    return filled


def _read_essentials(notion: NotionClient, page_id: str) -> int | None:
    blocks = collect_paginated_api(notion.blocks.children.list, block_id=page_id)
    section = None
    table_id = None
    for block in blocks:
        if block["type"] == "heading_2":
            section = "".join(
                p["plain_text"] for p in block["heading_2"]["rich_text"]
            ).strip()
        elif section == ESSENTIALS_HEADING and block["type"] == "table":
            table_id = block["id"]
            break
    rows = (
        collect_paginated_api(notion.blocks.children.list, block_id=table_id)
        if table_id
        else None
    )
    return count_essentials(blocks, rows)


@dataclass
class MorningReport:
    """아침 정리 결과"""

    text: str
    # 오늘 연락할 리드의 슬랙 스레드 ts
    due_thread_ts: list[str]


def format_due_line(lead: Lead, today: date) -> str:
    days = (today - lead.next_action_date).days
    when = "오늘" if days == 0 else f"{days}일 지남"
    return f"· {lead.name}: {lead.next_action or '다음 액션'} ({when})"


def format_report(
    today: date, due: list[Lead], missing: list[tuple[Lead, list[str]]]
) -> str | None:
    """아침 DM 글. 둘 다 없으면 None."""
    if not due and not missing:
        return None
    lines = [f"{today:%m/%d}({WEEKDAYS[today.weekday()]}) 아침 정리"]
    if due:
        lines += ["", f"*오늘 연락할 리드 {len(due)}건*"]
        lines += [format_due_line(lead, today) for lead in due]
    if missing:
        lines += ["", f"*자료가 빠진 리드 {len(missing)}건*"]
        lines += [
            f"· {lead.name} ({lead.status or '진행상태 없음'}): {', '.join(items)}"
            for lead, items in missing
        ]
    return "\n".join(lines)


def build_morning_report(notion: NotionClient, today: date) -> MorningReport | None:
    """리드 파이프라인 전체를 읽어 아침 정리를 만듭니다. 노션 호출이 동기입니다.

    Args:
        notion: 노션 클라이언트
        today: 오늘 (KST)

    Returns:
        MorningReport | None: 알릴 것이 없으면 None
    """
    pages = collect_paginated_api(
        notion.data_sources.query, data_source_id=LEAD_DATA_SOURCE_ID
    )
    leads = [lead_from_page(page) for page in pages]

    due = sorted(
        (lead for lead in leads if is_due(lead, today)),
        key=lambda lead: lead.next_action_date,
    )
    missing = []
    for lead in leads:
        filled = (
            _read_essentials(notion, lead.page_id)
            if needs_essentials_check(lead)
            else None
        )
        items = missing_items(lead, filled, today)
        if items:
            missing.append((lead, items))

    text = format_report(today, due, missing)
    if text is None:
        return None
    return MorningReport(
        text=text,
        due_thread_ts=[lead.slack_thread_ts for lead in due if lead.slack_thread_ts],
    )
