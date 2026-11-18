"""해커톤 문의 접수(문의지기)의 파싱·분류·견적과 접수·독촉 흐름 테스트

폼 글은 #t_제품_해커톤_문의 에 실제로 올라온 글을 그대로 옮겼습니다.
슬랙 클라이언트와 노션 호출은 가짜로 바꿔 끼웁니다.
"""

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import hackathon_inquiry as app_inquiry
from service.hackathon_consult import (
    ConsultExtract,
    call_log_number,
    pending_comments,
    plan_essentials,
    plan_intake,
    plan_properties,
)
from service.hackathon_inquiry import (
    KST,
    Submission,
    SUMMARY_MARKER,
    apply_continuations,
    customer_type,
    estimate_draft,
    is_business_hours,
    is_test_submission,
    is_valid_phone,
    notion_properties,
    parse_headcount,
    parse_schedule,
    parse_submission,
)

TRUNCATED_FORM = """해커톤 문의 새 제출
*제출 시각*
2026-11-10 11:44
*유입 경로*
운영사례 · ai-donghaeng-260919

*기관·학교명*
사단법인 임팩트비즈니스재단

*담당 선생님*
최한샘

*연락처*
01082260223

*대상*
학교

*규모*
최대 50명/ 10팀

*희망 시기*
2027년 1월 초

*문의 내용*
ㅇ 장소 확정 : 카카오AI캠퍼스
ㅇ 식대 확… (전체 내용은 댓글에서 확인)
어드민 링크: <https://admin.codle.io/form_submissions/137/show>"""

CONTINUATION = (
    "문의 내용\nㅇ 장소 확정 : 카카오AI캠퍼스\nㅇ 식대 확정 : 1인 25000원\n1안 &gt; 2안"
)

UNIVERSITY_FORM = """해커톤 문의 새 제출
*제출 시각*
2026-10-30 14:08
*유입 경로*
학교 랜딩

*기관·회사명*
건양대학교 글로컬캠퍼스(논산)

*담당자명*
사회과학학술원 행정직원 김연준

*연락처*
<tel:010-5731-3074|010-5731-3074>

*대상*
학교

*규모*
64명

*희망 시기*
2026.12.15(화) ~ 2026.12.18(금) 3박4일

*문의 내용*
캠프 기획안 확인해 주세요.
어드민 링크: <https://admin.codle.io/form_submissions/129/show>"""


def test_폼_글을_항목별로_읽고_잘린_항목은_댓글_전문으로_채운다():
    # When : 문의 내용이 잘린 원글을 읽고 폼 봇의 이어 단 댓글을 적용하면
    submission = parse_submission(TRUNCATED_FORM)
    assert submission.truncated == {"문의 내용"}
    apply_continuations(submission, [CONTINUATION])

    # Then : 항목 값과 어드민 링크가 나오고, 문의 내용은 엔티티를 푼 전문이 된다
    assert submission.org == "사단법인 임팩트비즈니스재단"
    assert submission.contact == "최한샘"
    assert submission.scale == "최대 50명/ 10팀"
    assert submission.admin_url == "https://admin.codle.io/form_submissions/137/show"
    assert submission.body.endswith("1안 > 2안")
    assert submission.truncated == set()


def test_폼_제출_글이_아니면_읽지_않는다():
    # When : 사람이 쓴 글을 넣으면
    # Then : None
    assert (
        parse_submission("이큅 이라는곳에서 해커톤 문의가 와서 메일 전달드렸습니다")
        is None
    )


@pytest.mark.parametrize(
    "replace",
    [
        ("admin.codle.io", "admin.dev.codle.io"),  # 개발 서버
        ("사단법인 임팩트비즈니스재단", "[테스트] 코들중학교"),  # 기관명
        ("01082260223", "<tel:010-0000-0000|010-0000-0000>"),  # 더미 연락처
    ],
)
def test_테스트_제출을_가린다(replace):
    # When : 개발 서버·테스트 기관명·더미 연락처 중 하나라도 있으면
    submission = parse_submission(TRUNCATED_FORM.replace(*replace))

    # Then : 테스트 제출이다
    assert is_test_submission(submission)


def test_실제_제출은_테스트가_아니다():
    # When : 실제 제출이면
    # Then : 테스트가 아니다
    assert not is_test_submission(parse_submission(TRUNCATED_FORM))


@pytest.mark.parametrize(
    "raw, valid",
    [
        ("010-2840-8161", True),
        ("01082260223", True),
        ("0629503894", True),  # 광주 지역번호
        ("010.5266.8831", True),
        ("010-33350-6861", False),  # 실제로 들어온 오타
        ("111111111", False),
    ],
)
def test_연락처_형식(raw, valid):
    # When : 연락처를 검사하면
    # Then : 자릿수가 어긋난 번호만 틀리다
    assert is_valid_phone(raw) is valid


def test_기관명에_대학이_있으면_대상이_학교여도_대학으로_본다():
    # When : 대학 담당자가 학교 랜딩에서 "학교"를 고르면
    submission = parse_submission(UNIVERSITY_FORM)

    # Then : 고객유형은 대학·사업단
    assert customer_type(submission) == "대학·사업단"


@pytest.mark.parametrize(
    "scale, expected",
    [
        ("30명", 30),
        ("30", 30),
        ("최대 50명/ 10팀", None),
        ("미정", None),
        ("대학생 20~30명 이상", None),
    ],
)
def test_규모는_숫자가_하나일_때만_인원으로_읽는다(scale, expected):
    # When : 규모를 읽으면
    # Then : 숫자가 하나이고 팀 단위가 아닐 때만 인원이다
    assert parse_headcount(scale) == expected


def test_희망_시기는_연월일이_있을_때만_날짜로_읽는다():
    # When : 기간, 하루, 연월만 있는 경우를 읽으면
    # Then : 기간은 시작·끝, 하루는 시작만, 연월만은 못 읽는다
    assert parse_schedule("2026.12.15(화) ~ 2026.12.18(금) 3박4일") == (
        date(2026, 12, 15),
        date(2026, 12, 18),
    )
    assert parse_schedule("2026.11.14.") == (date(2026, 11, 14), None)
    assert parse_schedule("2027년 1월 초") is None


@pytest.mark.parametrize(
    "customer, headcount, expected",
    [
        ("고교·K12", 30, "200만 원"),
        ("고교·K12", 50, "260만 원"),
        ("대학·사업단", 64, "236만 원"),
        ("기업", 30, "65만 원"),
        ("고교·K12", None, "규모 확인 후 산정"),
    ],
)
def test_견적_초안은_견적기준표_구간을_따른다(customer, headcount, expected):
    # When : 고객유형과 인원으로 견적을 내면
    # Then : 고교 40명 기본+인당 6만, 대학·기업은 운영형/플랫폼형 공식이다
    assert expected in estimate_draft(customer, headcount)


def test_노션_속성은_읽은_값만_넣고_못_읽은_값은_비고에_원문으로_둔다():
    # When : 규모·희망 시기를 못 읽는 제출로 속성을 만들면
    submission = parse_submission(TRUNCATED_FORM)
    props = notion_properties(submission, "notion-user", "https://slack/p1")

    # Then : 규모·희망일정 속성은 없고, 비고에 원문과 어드민·슬랙 링크가 있다
    assert "규모(명)" not in props
    assert "희망일정" not in props
    note = "".join(part["text"]["content"] for part in props["비고"]["rich_text"])
    assert "규모: 최대 50명/ 10팀" in note
    assert "희망 시기: 2027년 1월 초" in note
    assert "https://admin.codle.io/form_submissions/137/show" in note
    assert "https://slack/p1" in note
    assert props["최초 문의일"]["date"]["start"] == "2026-11-10T11:44:00+09:00"


def test_업무시간은_영업일_10시부터_19시_전까지다():
    # When : 영업일 9:59, 10:00, 19:00 과 휴일 12:00 을 보면
    day = datetime(2026, 11, 16, tzinfo=KST)

    # Then : 영업일 10:00 만 업무시간이다
    assert not is_business_hours(day.replace(hour=9, minute=59), True)
    assert is_business_hours(day.replace(hour=10), True)
    assert not is_business_hours(day.replace(hour=19), True)
    assert not is_business_hours(day.replace(hour=12), False)


# ---- 접수·독촉 흐름 ----

NOW = datetime(2026, 11, 17, 14, 0, tzinfo=KST)
FORM_TS = str((NOW - timedelta(minutes=5)).timestamp())


def _form_message(**overrides) -> dict:
    return {"ts": FORM_TS, "bot_id": "B07TA4Q38H4", "text": TRUNCATED_FORM, **overrides}


def _client(history: list[dict], replies: list[dict]) -> AsyncMock:
    client = AsyncMock()
    client.conversations_history.return_value = {"messages": history}
    client.conversations_replies.return_value = {"messages": replies}
    client.users_info.return_value = {"user": {"profile": {"status_text": ""}}}
    return client


@pytest.fixture
def env(monkeypatch):
    """업무시간, 노션 호출, 처리 기록을 고정합니다."""
    app_inquiry._seen.clear()
    app_inquiry._last_nudged.clear()
    app_inquiry._consult_scanned.clear()
    monkeypatch.setattr(app_inquiry, "_owner_away", False)
    monkeypatch.setattr(app_inquiry, "_bot_user_id", "UBOT")
    monkeypatch.setattr(app_inquiry, "business_hours_now", AsyncMock(return_value=True))
    created = []
    monkeypatch.setattr(app_inquiry, "_find_existing_lead", lambda s: None)
    monkeypatch.setattr(
        app_inquiry,
        "_create_lead",
        lambda s, ts, source: created.append(s.org) or "https://notion.so/lead",
    )
    return created


async def test_새_제출이면_노션에_등록하고_스레드에_정리_글로_담당자를_멘션한다(env):
    # When : 정리 글이 없는 폼 제출 스레드를 처리하면
    form = _form_message(reply_count=1)
    continuation = {"ts": "2", "bot_id": "B07TA4Q38H4", "text": CONTINUATION}
    client = _client([form], [form, continuation])
    await app_inquiry.poll_new_inquiries(client)

    # Then : 리드 하나를 만들고, 전문·노션 링크를 담아 이주현 님을 멘션한다
    assert env == ["사단법인 임팩트비즈니스재단"]
    text = client.chat_postMessage.call_args.kwargs["text"]
    assert text.startswith(SUMMARY_MARKER)
    assert f"<@{app_inquiry.OWNER_USER_ID}>" in text
    assert "1인 25000원" in text
    assert "https://notion.so/lead" in text


async def test_이미_정리_글이_있는_스레드는_다시_처리하지_않는다(env):
    # When : 스레드에 문의지기 정리 글이 이미 있으면
    form = _form_message(reply_count=1)
    summary = {"ts": "2", "bot_id": "B07TA4Q38H4", "text": f"{SUMMARY_MARKER}\n..."}
    client = _client([form], [form, summary])
    await app_inquiry.poll_new_inquiries(client)

    # Then : 노션도 슬랙도 건드리지 않는다
    assert env == []
    client.chat_postMessage.assert_not_called()


async def test_업무시간_외_접수는_멘션하지_않고_정리만_한다(env, monkeypatch):
    # When : 업무시간 외에 들어온 제출을 처리하면
    monkeypatch.setattr(
        app_inquiry, "business_hours_now", AsyncMock(return_value=False)
    )
    client = _client([_form_message()], [])
    await app_inquiry.poll_new_inquiries(client)

    # Then : 정리 글에 누구의 멘션도 없다
    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "<@" not in text
    assert "다음 업무일 10:00" in text


async def test_같은_기관이_노션에_있으면_긴급_재문의로_표시하고_새로_만들지_않는다(
    env, monkeypatch
):
    # When : 노션에 같은 기관 리드가 있으면
    monkeypatch.setattr(
        app_inquiry, "_find_existing_lead", lambda s: ("https://notion.so/old", False)
    )
    client = _client([_form_message()], [])
    await app_inquiry.poll_new_inquiries(client)

    # Then : 새 페이지 없이 기존 링크와 [긴급] 재문의를 단다
    assert env == []
    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "[긴급] 재문의" in text
    assert "https://notion.so/old" in text


async def test_담당자가_휴가면_김세윤_님을_멘션한다(env):
    # When : 이주현 님 상태가 휴가이면
    client = _client([_form_message()], [])
    client.users_info.return_value = {
        "user": {"profile": {"status_text": "휴가", "status_emoji": ":palm_tree:"}}
    }
    await app_inquiry.poll_new_inquiries(client)

    # Then : 김세윤 님을 멘션하고 이주현 님은 멘션하지 않는다
    text = client.chat_postMessage.call_args.kwargs["text"]
    assert f"<@{app_inquiry.BACKUP_USER_ID}>" in text
    assert f"<@{app_inquiry.OWNER_USER_ID}>" not in text


def _summary(ts: datetime, text: str | None = None, **overrides) -> dict:
    return {
        "ts": str(ts.timestamp()),
        "bot_id": "B07TA4Q38H4",
        "text": text
        or (
            f"{SUMMARY_MARKER}\n<@{app_inquiry.OWNER_USER_ID}> 새 해커톤 문의입니다.\n\n"
            "기관: 사단법인 임팩트비즈니스재단 (고교·K12)\n\n"
            "노션: <https://www.notion.so/lead-0123456789abcdef0123456789abcdef|리드 페이지>"
        ),
        **overrides,
    }


@pytest.fixture
def frozen_now(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(app_inquiry, "datetime", FrozenDatetime)


async def test_멘션_후_2시간_동안_반응이_없으면_DM_하고_4시간이_지나면_목표_초과를_붙인다(
    env, frozen_now
):
    # When : 5시간 전 접수, 멘션 3시간 전, 반응 없음
    form = _form_message(ts=str((NOW - timedelta(hours=5)).timestamp()), reply_count=1)
    client = _client([form], [form, _summary(NOW - timedelta(hours=3))])
    await app_inquiry.remind_unconfirmed(client)

    # Then : 이주현 님께 DM 하고 목표 초과를 표시한다
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == app_inquiry.OWNER_USER_ID
    assert "사단법인 임팩트비즈니스재단" in kwargs["text"]
    assert "목표 4시간 초과" in kwargs["text"]


async def test_담당자가_이모지를_누르면_독촉하지_않는다(env, frozen_now):
    # When : 정리 글에 이주현 님 이모지가 있으면
    form = _form_message(ts=str((NOW - timedelta(hours=5)).timestamp()), reply_count=1)
    summary = _summary(
        NOW - timedelta(hours=3),
        reactions=[{"name": "eyes", "users": [app_inquiry.OWNER_USER_ID]}],
    )
    client = _client([form], [form, summary])
    await app_inquiry.remind_unconfirmed(client)

    # Then : 아무것도 보내지 않는다
    client.chat_postMessage.assert_not_called()


async def test_업무시간_외_접수_건은_업무시간에_스레드에서_멘션한다(env, frozen_now):
    # When : 멘션 없이 정리만 해 둔 건을 업무시간에 보면
    form = _form_message(ts=str((NOW - timedelta(hours=12)).timestamp()), reply_count=1)
    summary = _summary(
        NOW - timedelta(hours=12), text=f"{SUMMARY_MARKER}\n업무시간 외 접수라 ..."
    )
    client = _client([form], [form, summary])
    await app_inquiry.remind_unconfirmed(client)

    # Then : DM 이 아니라 스레드에 담당자 멘션을 단다
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == app_inquiry.CHANNEL_ID
    assert kwargs["thread_ts"] == form["ts"]
    assert f"<@{app_inquiry.OWNER_USER_ID}>" in kwargs["text"]


async def test_휴가가_풀리면_휴가_동안_김세윤_님께_간_문의를_이주현_님께_한_번_알린다(
    env, frozen_now, monkeypatch
):
    # Given : 지난번에는 휴가였고, 김세윤 님이 확인한 문의가 하나 있다
    monkeypatch.setattr(app_inquiry, "_owner_away", True)
    form = _form_message(ts=str((NOW - timedelta(days=2)).timestamp()), reply_count=1)
    summary = _summary(
        NOW - timedelta(days=2),
        text=f"{SUMMARY_MARKER}\n<@{app_inquiry.BACKUP_USER_ID}> 새 해커톤 문의입니다.\n"
        "기관: 낭주중학교 (고교·K12)",
        reactions=[{"name": "eyes", "users": [app_inquiry.BACKUP_USER_ID]}],
    )
    client = _client([form], [form, summary])

    # When : 휴가가 풀린 뒤 처음 돌면
    await app_inquiry.remind_unconfirmed(client)

    # Then : 이주현 님께 그 문의를 DM 하고, 다음 회차에는 다시 보내지 않는다
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == app_inquiry.OWNER_USER_ID
    assert "휴가 동안" in kwargs["text"] and "낭주중학교" in kwargs["text"]
    client.chat_postMessage.reset_mock()
    await app_inquiry.remind_unconfirmed(client)
    client.chat_postMessage.assert_not_called()


# ---- F5 상담 댓글 → 노션 ----

CONSULT_PROPS = {
    "마지막 접촉일": {"type": "date", "date": None},
    "진행상태": {"type": "select", "select": {"name": "문의접수"}},
    "다음 액션": {"type": "rich_text", "rich_text": []},
    "다음 액션일": {"type": "date", "date": {"start": "2026-11-10"}},
    "규모(명)": {"type": "number", "number": 30},
    "희망일정": {"type": "date", "date": None},
    "견적규모(만원)": {"type": "number", "number": None},
}


def _extract(**overrides) -> ConsultExtract:
    values = {
        "is_consultation": True,
        "call_type": "첫 통화",
        "next_action": "견적 회신",
        "next_action_date": "2026-11-20",
        "headcount": None,
        "desired_start": None,
        "desired_end": None,
        "quote_manwon": None,
        **{f"item{i}": None for i in range(1, 8)},
    }
    return ConsultExtract(**{**values, **overrides})


def test_상담_댓글로_빈_칸만_채우고_다른_값은_바꾸지_않고_묻는다():
    # When : 노션 규모는 30명인데 댓글은 40명, 지난 다음 액션일이 남아 있으면
    plan = plan_properties(
        CONSULT_PROPS,
        _extract(headcount=40, desired_start="2026-12-15", quote_manwon=240),
        date(2026, 11, 16),
    )

    # Then : 빈 칸과 지난 약속은 채우고, 규모는 바꾸지 않고 확인을 청한다
    assert plan.updates["진행상태"] == {"select": {"name": "상담"}}
    assert plan.updates["마지막 접촉일"] == {"date": {"start": "2026-11-16"}}
    assert plan.updates["다음 액션일"] == {"date": {"start": "2026-11-20"}}
    assert plan.updates["다음 액션"]["rich_text"][0]["text"]["content"] == "견적 회신"
    assert plan.updates["희망일정"]["date"]["start"] == "2026-12-15"
    assert plan.updates["견적규모(만원)"] == {"number": 240}
    assert "규모(명)" not in plan.updates
    assert plan.conflicts == ["규모(명): 노션 30 / 댓글 40"]


def test_아직_오지_않은_다음_액션일이_댓글과_다르면_바꾸지_않는다():
    props = {
        **CONSULT_PROPS,
        "다음 액션일": {"type": "date", "date": {"start": "2026-11-25"}},
    }
    plan = plan_properties(props, _extract(), date(2026, 11, 16))
    assert "다음 액션일" not in plan.updates
    assert plan.conflicts == ["다음 액션일: 노션 11/25 / 댓글 11/20"]


def _row(block_id: str, number: str, label: str, answer: str) -> dict:
    cell = lambda text: (
        [{"plain_text": text, "text": {"content": text}}] if text else []
    )
    return {
        "id": block_id,
        "table_row": {"cells": [cell(number), cell(label), cell(answer)]},
    }


def test_필수_7항목은_빈_답_칸만_채우고_남은_빈_항목을_돌려준다():
    rows = [_row("h", "#", "항목", "답"), _row("r1", "1", "학년 / 인원", "")]
    rows += [_row("r4", "4", "예산 규모", "300만")]
    rows += [_row("r5", "5", "예산 출처", "")]
    updates, filled, empty = plan_essentials(
        rows, _extract(item1="고1 30명", item4="500만")
    )
    assert [row_id for row_id, _ in updates] == ["r1"]
    assert updates[0][1][2] == [{"text": {"content": "고1 30명"}}]
    assert filled == ["학년 / 인원"]
    assert empty == ["예산 출처"]


def test_통화_기록_번호는_템플릿_자리표시를_세지_않는다():
    assert call_log_number(["통화 1 · YYYY-MM-DD · 상담자"]) == 1
    assert (
        call_log_number(
            ["통화 1 · 2026-11-10 · 이주현", "통화 1 · YYYY-MM-DD · 상담자"]
        )
        == 2
    )


def test_옮길_댓글은_담당자가_정리_글_뒤에_달고_봇_이모지가_없는_것만():
    thread = [
        {"ts": "1.0"},
        {"ts": "2.0", "user": "UO", "text": "정리 글 전 댓글"},
        {"ts": "3.0", "bot_id": "B", "text": "정리 글"},
        {
            "ts": "4.0",
            "user": "UO",
            "text": "옮긴 댓글",
            "reactions": [{"name": "memo", "users": ["UBOT"]}],
        },
        {"ts": "5.0", "user": "UX", "text": "다른 사람"},
        {"ts": "6.0", "user": "UO", "text": "<@UBOT> 점검해줘"},
        {"ts": "7.0", "user": "UO", "text": "새 상담 내용"},
    ]
    pending = pending_comments(thread, "3.0", {"UO"}, "UBOT")
    assert [m["ts"] for m in pending] == ["7.0"]


@pytest.fixture
def consult_env(env, frozen_now, monkeypatch):
    """노션 상담 페이지 읽기·쓰기와 LLM 을 가짜로 둡니다."""
    written = {}
    monkeypatch.setattr(
        app_inquiry,
        "_read_consult_page",
        lambda page_id: {
            "properties": CONSULT_PROPS,
            "after": "intro",
            "toggle_titles": ["통화 1 · YYYY-MM-DD · 상담자"],
            "rows": [_row("r1", "1", "학년 / 인원", "")],
        },
    )
    monkeypatch.setattr(
        app_inquiry,
        "_write_consult_page",
        lambda page_id, after, block, props, rows: written.update(
            page_id=page_id, after=after, block=block, props=props, rows=rows
        ),
    )
    return written


def _consult_client(comment: dict) -> AsyncMock:
    form = _form_message(
        ts=str((NOW - timedelta(days=1)).timestamp()),
        reply_count=2,
        latest_reply=comment["ts"],
    )
    summary = _summary(NOW - timedelta(days=1))
    return _client([form], [form, summary, comment])


async def test_담당자의_상담_댓글을_노션_통화_기록에_옮기고_스레드에_결과를_답한다(
    consult_env, monkeypatch
):
    # Given : 10분 전에 이주현 님이 상담 댓글을 달았다
    monkeypatch.setattr(
        app_inquiry,
        "extract_consult",
        AsyncMock(return_value=_extract(item1="고1 30명")),
    )
    comment = {
        "ts": str((NOW - timedelta(minutes=10)).timestamp()),
        "user": app_inquiry.OWNER_USER_ID,
        "text": "첫 통화. 고1 30명, 11/20까지 견적 회신 드리기로",
    }
    client = _consult_client(comment)

    # When
    await app_inquiry.sync_consult_comments(client)

    # Then : 맨 위에 통화 1 을 넣고, 댓글에 📝 를 달고, 결과를 답한다
    assert consult_env["page_id"] == "0123456789abcdef0123456789abcdef"
    assert consult_env["after"] == "intro"
    title = consult_env["block"]["heading_3"]["rich_text"][0]["text"]["content"]
    assert title == "통화 1 · 2026-11-17 · 이주현"
    assert consult_env["props"]["진행상태"] == {"select": {"name": "상담"}}
    assert [row_id for row_id, _ in consult_env["rows"]] == ["r1"]
    client.reactions_add.assert_called_once_with(
        channel=app_inquiry.CHANNEL_ID, timestamp=comment["ts"], name="memo"
    )
    reply = client.chat_postMessage.call_args.kwargs["text"]
    assert "노션에 기록했습니다" in reply and "통화 기록 1 추가" in reply


async def test_막_단_댓글은_이어_쓸_수_있게_조금_기다린다(consult_env, monkeypatch):
    extract = AsyncMock()
    monkeypatch.setattr(app_inquiry, "extract_consult", extract)
    comment = {
        "ts": str((NOW - timedelta(minutes=1)).timestamp()),
        "user": app_inquiry.OWNER_USER_ID,
        "text": "첫 통화 내용",
    }
    await app_inquiry.sync_consult_comments(_consult_client(comment))
    extract.assert_not_called()
    assert consult_env == {}


async def test_상담이_아닌_짧은_댓글은_노션에_쓰지_않고_표시만_한다(
    consult_env, monkeypatch
):
    monkeypatch.setattr(
        app_inquiry,
        "extract_consult",
        AsyncMock(return_value=_extract(is_consultation=False)),
    )
    comment = {
        "ts": str((NOW - timedelta(minutes=10)).timestamp()),
        "user": app_inquiry.OWNER_USER_ID,
        "text": "제가 연락드릴게요",
    }
    client = _consult_client(comment)
    await app_inquiry.sync_consult_comments(client)
    assert consult_env == {}
    client.reactions_add.assert_called_once_with(
        channel=app_inquiry.CHANNEL_ID, timestamp=comment["ts"], name="heavy_minus_sign"
    )
    client.chat_postMessage.assert_not_called()


# ---- 로봇 도구 ----


async def test_문의_등록은_붙인_문의를_인바운드_메일로_접수한다(env, monkeypatch):
    sources = []
    monkeypatch.setattr(
        app_inquiry,
        "_create_lead",
        lambda s, ts, source: sources.append(source) or "https://notion.so/lead",
    )
    root = {"ts": "1.0", "user": "UX", "text": "메일로 온 문의 붙입니다 ..."}
    client = _client([], [root])
    tools = app_inquiry.get_hackathon_tools(client, app_inquiry.CHANNEL_ID, "1.0", "UX")
    register = next(t for t in tools if t.name == "register_hackathon_inquiry")

    text = await register.ainvoke(
        {"org": "덕현고등학교", "contact": "김교사", "target": "학교", "scale": "30명"}
    )

    assert sources == ["인바운드-메일"]
    assert "기관: 덕현고등학교 (고교·K12)" in text
    assert client.chat_postMessage.call_args.kwargs["thread_ts"] == "1.0"


def test_문의지기_도구는_문의_채널과_담당자_DM_에서만_준다():
    client = AsyncMock()
    names = lambda tools: sorted(t.name for t in tools)
    assert names(
        app_inquiry.get_hackathon_tools(client, app_inquiry.CHANNEL_ID, "1.0", "UX")
    ) == ["check_hackathon_leads", "register_hackathon_inquiry"]
    assert names(
        app_inquiry.get_hackathon_tools(
            client, "D123", "1.0", app_inquiry.OWNER_USER_ID
        )
    ) == ["check_hackathon_leads"]
    assert app_inquiry.get_hackathon_tools(client, "C999", "1.0", "UX") == []


def _motive(text: str = "계기 (사업명, 목적, 누가 원하는지):") -> dict:
    return {
        "id": "m",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"plain_text": text}]},
    }


def _intake_rows() -> list[dict]:
    return [
        _row("h", "#", "항목", "답"),
        _row("r1", "1", "학년 / 인원", ""),
        _row("r2", "2", "차시·시간 / 정규·방과후", ""),
        _row("r3", "3", "희망 날짜·시기 / 피할 기간", ""),
        _row("r4", "4", "예산 규모", ""),
    ]


def test_새_상담_페이지에는_폼에_적힌_대상_규모_희망시기_문의내용만_옮긴다():
    submission = Submission(
        title="해커톤 문의",
        fields={
            "대상": "고등학교 2학년",
            "규모": "60명",
            "희망 시기": "12월 셋째 주",
            "문의 내용": "진로 주간 행사로 해커톤을 하고 싶습니다.",
        },
        admin_url=None,
    )
    updates, motive = plan_intake(_intake_rows(), _motive(), submission)

    # 1·3번만 채우고, 폼에 없는 차시·예산은 비워 둔다
    assert [(row_id, cells[2][0]["text"]["content"]) for row_id, cells in updates] == [
        ("r1", "고등학교 2학년 / 60명"),
        ("r3", "12월 셋째 주"),
    ]
    assert motive["rich_text"][0]["text"]["content"] == (
        "계기 (사업명, 목적, 누가 원하는지):\n진로 주간 행사로 해커톤을 하고 싶습니다."
    )


def test_폼에_없거나_슬랙에서_잘린_값은_적지_않는다():
    submission = Submission(
        title="해커톤 문의",
        fields={"규모": "60명", "문의 내용": "앞부분만..."},
        admin_url=None,
        truncated={"문의 내용"},
    )
    updates, motive = plan_intake(_intake_rows(), _motive(), submission)

    # 대상이 없으면 규모만, 희망 시기가 없으면 3번은 그대로, 잘린 문의 내용은 옮기지 않는다
    assert [row_id for row_id, _ in updates] == ["r1"]
    assert updates[0][1][2][0]["text"]["content"] == "60명"
    assert motive is None


def test_사람이_이미_적은_칸은_덮어쓰지_않는다():
    submission = Submission(
        title="해커톤 문의",
        fields={"대상": "고2", "희망 시기": "12월", "문의 내용": "문의"},
        admin_url=None,
    )
    rows = _intake_rows()
    rows[1] = _row("r1", "1", "학년 / 인원", "고2 58명 (통화 확인)")
    updates, motive = plan_intake(
        rows, _motive("계기 (사업명, 목적, 누가 원하는지): 진로 주간"), submission
    )
    assert [row_id for row_id, _ in updates] == ["r3"]
    assert motive is None


def test_템플릿_본문이_생길_때까지_기다렸다가_적는다(monkeypatch):
    heading = {
        "id": "h2",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"plain_text": "1. 필수 7항목"}]},
    }
    table = {"id": "t", "type": "table", "table": {}}
    pages = {"page": [[], [heading, table, _motive()]], "t": [_intake_rows()]}
    calls = {"page": 0, "t": 0}

    def list_children(block_id, start_cursor=None):
        result = pages[block_id][min(calls[block_id], len(pages[block_id]) - 1)]
        calls[block_id] += 1
        return {"results": result, "has_more": False, "next_cursor": None}

    notion = MagicMock()
    notion.blocks.children.list.side_effect = list_children
    monkeypatch.setattr(app_inquiry, "notion", notion)
    monkeypatch.setattr(app_inquiry.time, "sleep", lambda s: None)

    submission = Submission(
        title="해커톤 문의", fields={"희망 시기": "12월"}, admin_url=None
    )
    app_inquiry._fill_intake("page", submission)

    # 처음엔 본문이 비어 다시 읽고, 표가 생기자 3번만 적는다
    assert calls["page"] == 2
    notion.blocks.update.assert_called_once()
    assert notion.blocks.update.call_args.kwargs["block_id"] == "r3"
