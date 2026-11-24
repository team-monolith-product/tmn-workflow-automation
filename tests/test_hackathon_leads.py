"""해커톤 리드 아침 정리(F6·F7) 테스트

노션 페이지는 리드 파이프라인 속성 모양을 그대로 흉내 낸 가짜입니다.
"""

from datetime import date
from unittest.mock import MagicMock

from service.hackathon_leads import (
    Lead,
    build_morning_report,
    count_essentials,
    format_report,
    is_due,
    lead_from_page,
    missing_items,
)

TODAY = date(2026, 11, 17)


def _lead(status: str, empty: set[str] = frozenset(), **overrides) -> Lead:
    values = {
        "page_id": "p",
        "name": "가나고등학교",
        "status": status,
        "next_action": "",
        "next_action_date": None,
        "last_contact": TODAY,
        "empty": set(empty),
        "slack_thread_ts": None,
    }
    return Lead(**{**values, **overrides})


def test_다음_액션일이_오늘이거나_지난_열린_리드가_오늘_연락할_리드다():
    assert is_due(_lead("상담", next_action_date=TODAY), TODAY)
    assert is_due(_lead("협의", next_action_date=date(2026, 11, 15)), TODAY)
    assert not is_due(_lead("상담", next_action_date=date(2026, 11, 18)), TODAY)
    assert not is_due(_lead("수주·계약", next_action_date=TODAY), TODAY)


def test_수주_계약_리드는_계산서_발행에_필요한_자료를_챙긴다():
    lead = _lead("수주·계약", {"📎 보낸 견적서", "확정금액(만원)", "수주확률"})
    assert missing_items(lead, 7, TODAY) == [
        "보낸 견적서 없음 ← 계산서 발행 전 필요",
        "확정금액(만원) 없음",
        "수주확률 없음",
    ]


def test_상담_리드는_필수_7항목_채운_수를_알린다():
    assert missing_items(_lead("상담"), 3, TODAY) == ["필수 7항목 3/7"]
    # 템플릿 이전 페이지라 표가 없으면 점검하지 않는다
    assert missing_items(_lead("상담"), None, TODAY) == []


def test_무산_리드는_사유와_이탈_단계만_본다():
    lead = _lead("무산", {"무산 사유", "이탈 단계", "📎 보낸 견적서"})
    assert missing_items(lead, None, TODAY) == ["무산 사유 없음", "이탈 단계 없음"]


def test_14일_넘게_연락이_없는데_다음_액션일이_없으면_알린다():
    lead = _lead("문의접수", last_contact=date(2026, 11, 1))
    assert missing_items(lead, None, TODAY) == ["마지막 접촉 16일 전, 다음 액션일 없음"]


def test_알릴_것이_없으면_글을_만들지_않는다():
    assert format_report(TODAY, [], []) is None


def test_7항목_모두_확인을_체크했으면_7로_센다():
    blocks = [
        {
            "type": "heading_2",
            "heading_2": {"rich_text": [{"plain_text": "1. 필수 7항목"}]},
        },
        {
            "type": "to_do",
            "to_do": {
                "rich_text": [{"plain_text": "7항목 모두 확인"}],
                "checked": True,
            },
        },
    ]
    assert count_essentials(blocks, []) == 7


def _page(status: str, **props) -> dict:
    def text(value: str) -> list:
        return [{"plain_text": value}] if value else []

    base = {
        "고객·기관명": {"type": "title", "title": text("예시대 글로컬캠퍼스")},
        "진행상태": {"type": "select", "select": {"name": status}},
        "다음 액션": {"type": "rich_text", "rich_text": text("기획안 검토 결과 확인")},
        "다음 액션일": {"type": "date", "date": {"start": "2026-11-15"}},
        "마지막 접촉일": {"type": "date", "date": {"start": "2026-11-10"}},
        "비고": {
            "type": "rich_text",
            "rich_text": text(
                "슬랙: https://monolith-keb2010.slack.com/archives/C0B0BR3CY4T/p1794299497612819"
            ),
        },
        "📎 보낸 견적서": {"type": "files", "files": []},
        "📎 보낸 설명서·제안서": {"type": "files", "files": []},
        "견적규모(만원)": {"type": "number", "number": None},
        "수주확률": {"type": "select", "select": None},
        "희망일정": {"type": "date", "date": None},
        "확정금액(만원)": {"type": "number", "number": None},
        "🗓 행사일정(확정)": {"type": "date", "date": None},
        "📍 행사 장소": {"type": "rich_text", "rich_text": []},
        "무산 사유": {"type": "select", "select": None},
        "이탈 단계": {"type": "select", "select": None},
    }
    return {"id": "page-1", "properties": {**base, **props}}


def test_노션_페이지에서_슬랙_스레드를_읽는다():
    lead = lead_from_page(_page("상담"))
    assert lead.slack_thread_ts == "1794299497.612819"
    assert lead.next_action_date == date(2026, 11, 15)


def test_아침_정리는_오늘_연락할_리드와_빠진_자료를_한_글로_묶는다():
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [_page("문의접수")],
        "has_more": False,
        "next_cursor": None,
    }
    report = build_morning_report(notion, TODAY)
    assert "11/17(화) 아침 정리" in report.text
    assert "예시대 글로컬캠퍼스: 기획안 검토 결과 확인 (2일 지남)" in report.text
    assert report.due_thread_ts == ["1794299497.612819"]
