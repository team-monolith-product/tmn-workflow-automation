"""
해커톤 문의 담당자 챙기기

#t_제품_해커톤_문의 에 폼 제출 글이 올라오면 스레드에 정리 글을 달고 담당자를
멘션하며, 노션 「🗂 리드 파이프라인」에 리드를 등록합니다. 담당자가 이모지로
확인하지 않으면 업무시간 안에서 2시간마다 DM 합니다(F1~F4). 담당자가 스레드에
상담 내용을 댓글로 달면 노션 상담 페이지에 옮깁니다(F5). 아침 정리(F6·F7)는
scripts/notify_hackathon_leads.py 입니다.

이벤트가 아니라 채널을 주기적으로 읽습니다. 폼 글을 올리는 계정이 대표 봇
자신이라 Bolt 가 자기 메시지 이벤트를 버리기 때문입니다(IgnoringSelfEvents).
이 미들웨어를 끄면 대표 봇의 모든 리스너가 자기 글을 받게 되므로 건드리지 않습니다.
주기적으로 읽으면 프로세스가 내려가 있던 동안 들어온 제출도 다시 잡힙니다.

처리 여부는 따로 저장하지 않고 스레드에서 읽습니다. 정리 글 첫 줄에
SUMMARY_MARKER 가 있으면 처리한 것입니다. 노션은 비고의 어드민 링크로 중복 등록을 막습니다.
상담 댓글은 봇이 단 📝(옮김)·➖(상담 아님) 이모지로 처리 여부를 읽습니다.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from langchain_core.tools import StructuredTool, tool
from langchain_openai import ChatOpenAI
from notion_client.errors import HTTPResponseError, RequestTimeoutError
from notion_client.helpers import collect_paginated_api
from slack_sdk.web.async_client import AsyncWebClient

from service.business_days import is_business_day
from service.hackathon_consult import (
    EXTRACT_SYSTEM_PROMPT,
    RECORDED_REACTION,
    SKIPPED_REACTION,
    ConsultExtract,
    ConsultLayout,
    call_log_block,
    call_log_number,
    extract_prompt,
    format_record_reply,
    parse_consult_layout,
    pending_comments,
    plan_essentials,
    plan_intake,
    plan_properties,
)
from service.hackathon_inquiry import (
    BACKUP_USER_ID,
    CHANNEL_ID,
    CONSULT_TEMPLATE_ID,
    KST,
    LEAD_DATA_SOURCE_ID,
    OWNER_NOTION_ID,
    OWNER_USER_ID,
    SUMMARY_MARKER,
    USER_NAMES,
    Submission,
    apply_continuations,
    form_text,
    customer_type,
    estimate_draft,
    format_phone,
    format_summary,
    is_business_hours,
    is_test_submission,
    is_vacation_profile,
    is_valid_phone,
    notion_properties,
    parse_headcount,
    parse_submission,
    permalink,
    summary_notion_url,
    summary_org,
)
from service.hackathon_leads import build_morning_report
from service.llm import DEFAULT_MODEL
from .common import notion

# 이 시각 이전 제출은 사람이 이미 처리했다. 배포 직후 지난 글에 일제히 답하지 않도록.
START_AT = datetime(2026, 11, 16, 14, 0, tzinfo=KST)
# 폼 글을 찾는 범위. 주말·연휴 동안 프로세스가 내려가 있어도 잡히도록 넉넉히.
LOOKBACK = timedelta(days=5)
# 확인을 기다리는 범위. 이보다 오래된 건은 더 독촉하지 않는다.
REMIND_WINDOW = timedelta(days=14)
REMIND_INTERVAL = timedelta(hours=2)
FIRST_CONTACT_TARGET = timedelta(hours=4)
# 상담 댓글을 읽는 범위. 상담은 문의 뒤 몇 주에 걸쳐 이어진다.
CONSULT_WINDOW = timedelta(days=120)
# 댓글을 여러 개 이어 다는 동안 기다린다. 마지막 댓글 뒤 이만큼 조용하면 한 번에 옮긴다.
CONSULT_SETTLE = timedelta(minutes=3)
CONSULT_AUTHORS = {OWNER_USER_ID, BACKUP_USER_ID}

# 폼 글 ts. 한 번 본 글은 다시 스레드를 읽지 않는다. 재시작하면 비지만
# 스레드의 SUMMARY_MARKER 로 다시 거른다.
_seen: set[str] = set()
# 정리 글 ts -> 마지막으로 DM 한 시각. 재시작하면 비어 한 번 일찍 DM 할 수 있다.
_last_nudged: dict[str, datetime] = {}
# 원글 ts -> 상담 댓글을 다 옮겼을 때의 latest_reply. 새 댓글이 없는 스레드는 다시 읽지 않는다.
_consult_scanned: dict[str, str] = {}
# 이주현 님이 휴가였는지. 휴가가 풀리는 순간 몰아서 안내한다. 재시작하면 처음 본 상태부터 센다.
_owner_away = False
_bot_user_id: str | None = None


def ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=KST)


async def business_hours_now() -> bool:
    """지금이 업무시간인지. 공휴일 조회가 동기 HTTP 라 스레드에서 돈다."""
    now = datetime.now(KST)
    business_day = await asyncio.to_thread(is_business_day, now.date())
    return is_business_hours(now, business_day)


async def is_on_vacation(client: AsyncWebClient, user_id: str) -> bool:
    """슬랙 상태 메시지에 휴가 또는 🌴 가 있는지"""
    profile = (await client.users_info(user=user_id))["user"]["profile"]
    return is_vacation_profile(profile)


async def resolve_assignee(client: AsyncWebClient) -> tuple[str, bool]:
    """이번 문의를 받을 사람

    Returns:
        tuple[str, bool]: (슬랙 사용자 ID, 담당자가 휴가라 대리인지)
    """
    if await is_on_vacation(client, OWNER_USER_ID):
        return BACKUP_USER_ID, True
    return OWNER_USER_ID, False


async def bot_user_id(client: AsyncWebClient) -> str:
    """대표 봇 자신의 사용자 ID"""
    global _bot_user_id
    if _bot_user_id is None:
        _bot_user_id = (await client.auth_test())["user_id"]
    return _bot_user_id


def _find_existing_lead(submission: Submission) -> tuple[str, bool] | None:
    """노션에서 같은 제출 또는 같은 기관의 리드를 찾습니다.

    Returns:
        tuple[str, bool] | None: (페이지 URL, 같은 제출인지). 없으면 None
    """
    if submission.admin_url:
        same = notion.data_sources.query(
            data_source_id=LEAD_DATA_SOURCE_ID,
            filter={
                "property": "비고",
                "rich_text": {"contains": submission.admin_url},
            },
            page_size=1,
        )["results"]
        if same:
            return same[0]["url"], True

    conditions = []
    if submission.org:
        conditions.append(
            {"property": "고객·기관명", "title": {"equals": submission.org}}
        )
    if submission.phone:
        for phone in {submission.phone, format_phone(submission.phone)}:
            conditions.append({"property": "연락처", "phone_number": {"equals": phone}})
    if not conditions:
        return None
    found = notion.data_sources.query(
        data_source_id=LEAD_DATA_SOURCE_ID,
        filter={"or": conditions},
        page_size=1,
    )["results"]
    if found:
        return found[0]["url"], False
    return None


def _create_lead(submission: Submission, slack_ts: str, source: str) -> str:
    """리드 페이지를 상담 페이지 템플릿으로 만들고 URL 을 돌려줍니다."""
    page = notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": LEAD_DATA_SOURCE_ID},
        properties=notion_properties(
            submission, OWNER_NOTION_ID, permalink(slack_ts), source
        ),
        template={"type": "template_id", "template_id": CONSULT_TEMPLATE_ID},
    )
    _fill_intake(page["id"], submission)
    return page["url"]


# 노션은 템플릿 본문을 페이지를 만든 뒤에 따로 채운다. 표가 생길 때까지 기다리는 횟수·간격.
TEMPLATE_WAIT_TRIES = 10
TEMPLATE_WAIT_SECONDS = 2


def _fill_intake(page_id: str, submission: Submission) -> None:
    """새 상담 페이지의 필수 7항목 1·3번과 「계기」에 폼 내용을 적습니다.

    스레드에서 to_thread 로 돌므로 time.sleep 이 루프를 막지 않습니다.
    """
    for _ in range(TEMPLATE_WAIT_TRIES):
        layout = parse_consult_layout(
            collect_paginated_api(notion.blocks.children.list, block_id=page_id)
        )
        if layout.essentials_table_id:
            break
        time.sleep(TEMPLATE_WAIT_SECONDS)
    else:
        print(
            f"[hackathon_inquiry] 템플릿 본문이 아직 없어 미리 적기 건너뜀: {page_id}"
        )
        return

    rows = collect_paginated_api(
        notion.blocks.children.list, block_id=layout.essentials_table_id
    )
    row_updates, motive = plan_intake(rows, layout.motive_block, submission)
    for row_id, cells in row_updates:
        notion.blocks.update(block_id=row_id, table_row={"cells": cells})
    if motive:
        notion.blocks.update(block_id=layout.motive_block["id"], paragraph=motive)


def _ensure_lead(
    submission: Submission, slack_ts: str, source: str
) -> tuple[str, str | None, str | None]:
    """문의를 노션 리드에 잇습니다. 같은 제출이나 같은 기관이 있으면 새로 만들지 않습니다.

    Returns:
        tuple: (리드 페이지 URL, 링크 옆 설명, 정리 글 맨 앞 표시)
    """
    existing = _find_existing_lead(submission)
    if existing is None:
        return _create_lead(submission, slack_ts, source), None, None
    url, same_submission = existing
    if same_submission:
        return url, "이미 등록됨", None
    return url, "기존 페이지, 새로 만들지 않음", "[긴급] 재문의"


async def handle_submission(
    client: AsyncWebClient, message: dict, replies: list[dict]
) -> None:
    """폼 제출 한 건을 처리합니다.

    Args:
        client: 대표 봇 클라이언트
        message: 폼 원글
        replies: 원글을 뺀 스레드 댓글, 오래된 것부터
    """
    submission = parse_submission(form_text(message))
    apply_continuations(
        submission,
        [form_text(r) for r in replies if r.get("bot_id") == message["bot_id"]],
    )

    if is_test_submission(submission):
        print(f"[hackathon_inquiry] 테스트 제출 건너뜀: {submission.admin_url}")
        return
    await process_submission(client, submission, message["ts"], "인바운드-폼")


async def process_submission(
    client: AsyncWebClient, submission: Submission, thread_ts: str, source: str
) -> str:
    """문의 한 건을 접수합니다 (F1 정리·멘션, F2 노션 등록, F3 견적 초안).

    Args:
        client: 대표 봇 클라이언트
        submission: 문의
        thread_ts: 정리 글을 달 원글 ts
        source: 노션 유입경로

    Returns:
        str: 스레드에 단 정리 글
    """
    business_hours = await business_hours_now()
    assignee, is_backup = await resolve_assignee(client)
    flags: list[str] = []
    # 노션이 실패해도 담당자 멘션은 나가야 한다. 정리 글에 실패를 적어 올린 뒤
    # 오류를 그대로 다시 던진다. 정리 글이 달렸으므로 다음 폴링은 이 건을 건너뛴다.
    notion_url = notion_note = None
    notion_error = None
    try:
        notion_url, notion_note, flag = await asyncio.to_thread(
            _ensure_lead, submission, thread_ts, source
        )
        if flag:
            flags.append(flag)
    except (HTTPResponseError, RequestTimeoutError) as error:
        notion_error = error
        flags.append("[노션 등록 실패, 직접 등록 필요]")
    estimate = estimate_draft(
        customer_type(submission), parse_headcount(submission.scale)
    )
    if submission.phone and not is_valid_phone(submission.phone):
        flags.append("[연락처 확인]")

    text = format_summary(
        submission,
        mention_user_id=assignee if business_hours else None,
        deferred=not business_hours,
        notion_url=notion_url,
        notion_note=notion_note,
        estimate=estimate,
        flags=flags,
        backup=is_backup,
    )
    await client.chat_postMessage(
        channel=CHANNEL_ID, thread_ts=thread_ts, text=text, unfurl_links=False
    )
    print(f"[hackathon_inquiry] 접수 처리: {submission.org} → {notion_url}")
    if notion_error:
        raise notion_error
    return text


async def _thread(client: AsyncWebClient, ts: str) -> list[dict]:
    """스레드 전체. 첫 원소가 원글."""
    messages: list[dict] = []
    cursor = None
    while True:
        response = await client.conversations_replies(
            channel=CHANNEL_ID, ts=ts, cursor=cursor, limit=200
        )
        messages += response["messages"]
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return messages


async def _root_messages(client: AsyncWebClient, since: datetime) -> list[dict]:
    """since 이후 채널에 올라온 원글, 오래된 것부터"""
    oldest = max(since, START_AT).timestamp()
    messages: list[dict] = []
    cursor = None
    while True:
        response = await client.conversations_history(
            channel=CHANNEL_ID, oldest=str(oldest), cursor=cursor, limit=200
        )
        messages += response["messages"]
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return sorted(messages, key=lambda m: float(m["ts"]))


async def _form_messages(client: AsyncWebClient, since: datetime) -> list[dict]:
    """since 이후 채널에 올라온 폼 제출 원글, 오래된 것부터"""
    return [
        m
        for m in await _root_messages(client, since)
        if m.get("bot_id") and parse_submission(form_text(m)) is not None
    ]


async def _inquiry_threads(client: AsyncWebClient, since: datetime):
    """정리 글이 달린 문의 스레드. 폼 글과 사람이 붙인 문의(문의 등록) 모두.

    Yields:
        tuple[dict, list[dict], list[dict]]: (원글, 스레드 전체, 정리 글들)
    """
    for message in await _root_messages(client, since):
        if not message.get("reply_count"):
            continue
        thread = await _thread(client, message["ts"])
        summaries = [m for m in thread if _is_summary(m)]
        if summaries:
            yield message, thread, summaries


def _is_summary(message: dict) -> bool:
    return message.get("text", "").startswith(SUMMARY_MARKER)


async def poll_new_inquiries(client: AsyncWebClient) -> None:
    """새 폼 제출을 찾아 처리합니다. 스케줄러가 1분마다 부릅니다."""
    now = datetime.now(KST)
    for message in await _form_messages(client, now - LOOKBACK):
        if message["ts"] in _seen:
            continue
        thread = (
            await _thread(client, message["ts"])
            if message.get("reply_count")
            else [message]
        )
        if not any(_is_summary(m) for m in thread):
            await handle_submission(client, message, thread[1:])
        _seen.add(message["ts"])


def _acknowledged(
    thread: list[dict], summaries: list[dict], responders: set[str]
) -> bool:
    """담당자가 원글이나 봇 글(정리 글, 나중에 단 멘션 글)에 이모지를 눌렀거나
    첫 정리 글 뒤에 댓글을 달았는지. 알림은 나중에 단 멘션 글에서 오기도 한다."""
    for message in [thread[0], *summaries]:
        for reaction in message.get("reactions", []):
            if responders & set(reaction["users"]):
                return True
    return any(
        m.get("user") in responders and float(m["ts"]) > float(summaries[0]["ts"])
        for m in thread
    )


async def remind_unconfirmed(client: AsyncWebClient) -> None:
    """확인 안 된 문의를 챙깁니다 (F4). 스케줄러가 10분마다 부릅니다.

    - 업무시간 외에 접수해 멘션을 미룬 건: 스레드에서 담당자를 멘션
    - 멘션 후 2시간 동안 반응이 없는 건: 담당자에게 DM, 이후 2시간마다
    - 이주현 님 휴가가 풀리면: 휴가 동안 김세윤 님께 간 문의를 DM 한 번
    업무시간이 아니면 아무것도 하지 않습니다.
    """
    global _owner_away
    if not await business_hours_now():
        return

    now = datetime.now(KST)
    assignee, is_backup = await resolve_assignee(client)
    returned = _owner_away and not is_backup
    responders = {OWNER_USER_ID, BACKUP_USER_ID}
    pending: list[str] = []
    handed_over: list[str] = []

    async for message, thread, summaries in _inquiry_threads(
        client, now - REMIND_WINDOW
    ):
        summary = summaries[0]
        org = summary_org(summary["text"])
        received = ts_to_datetime(message["ts"])
        if returned and any(f"<@{BACKUP_USER_ID}>" in m["text"] for m in summaries):
            handed_over.append(
                f"· {org} (접수 {received:%m/%d %H:%M}) → <{permalink(message['ts'])}|스레드>"
            )
        if _acknowledged(thread, summaries, responders):
            continue

        mentions = [m for m in summaries if f"<@{assignee}>" in m["text"]]
        if not mentions:
            # 업무시간 외 접수였거나, 그사이 담당이 바뀌었다(휴가)
            text = f"{SUMMARY_MARKER}\n<@{assignee}> 확인이 필요한 해커톤 문의입니다."
            if is_backup:
                text += " (이주현 님 휴가 중)"
            await client.chat_postMessage(
                channel=CHANNEL_ID, thread_ts=message["ts"], text=text
            )
            continue

        last = max(
            ts_to_datetime(mentions[-1]["ts"]),
            _last_nudged.get(summary["ts"], datetime.min.replace(tzinfo=KST)),
        )
        if now - last < REMIND_INTERVAL:
            continue
        elapsed = now - received
        hours = int(elapsed.total_seconds() // 3600)
        line = f"· {org} (접수 {received:%m/%d %H:%M}, {hours}시간 경과"
        if elapsed >= FIRST_CONTACT_TARGET and not is_backup:
            line += ", 목표 4시간 초과"
        pending.append(line + f") → <{permalink(message['ts'])}|스레드>")
        _last_nudged[summary["ts"]] = now

    if handed_over:
        text = "\n".join(
            [
                f"휴가 동안 김세윤 님께 안내한 해커톤 문의가 {len(handed_over)}건 있습니다."
            ]
            + handed_over
        )
        await client.chat_postMessage(
            channel=OWNER_USER_ID, text=text, unfurl_links=False
        )
        print(f"[hackathon_inquiry] 휴가 복귀 안내 {len(handed_over)}건")
    # 안내를 보낸 뒤에 바꾼다. 도중에 실패하면 다음 회차에 다시 안내한다.
    _owner_away = is_backup

    if not pending:
        return
    text = "\n".join(
        [f"아직 확인 안 된 해커톤 문의가 {len(pending)}건 있습니다."]
        + pending
        + ["확인하면 스레드에 이모지를 눌러 주세요."]
    )
    await client.chat_postMessage(channel=assignee, text=text, unfurl_links=False)
    print(f"[hackathon_inquiry] 독촉 DM {len(pending)}건 → {assignee}")


# ---- F5 상담 댓글 → 노션 ----


def _page_id(notion_url: str) -> str:
    """노션 페이지 URL 끝의 32자리 ID"""
    return notion_url.split("?")[0].rstrip("/").split("-")[-1].split("/")[-1]


@dataclass
class ConsultPage:
    """상담 페이지에서 F5 가 읽는 것"""

    properties: dict
    layout: ConsultLayout
    # 필수 7항목 표의 행들. 템플릿 이전 페이지라 표가 없으면 빈 목록
    essential_rows: list[dict]


def _read_consult_page(page_id: str) -> ConsultPage:
    """상담 페이지의 속성과 본문 구조를 읽습니다."""
    properties = notion.pages.retrieve(page_id=page_id)["properties"]
    layout = parse_consult_layout(
        collect_paginated_api(notion.blocks.children.list, block_id=page_id)
    )
    rows = (
        collect_paginated_api(
            notion.blocks.children.list, block_id=layout.essentials_table_id
        )
        if layout.essentials_table_id
        else []
    )
    return ConsultPage(properties, layout, rows)


def _write_consult_page(
    page_id: str,
    after: str | None,
    log_block: dict,
    property_updates: dict,
    row_updates: list[tuple[str, list]],
) -> None:
    """상담 페이지에 씁니다. 통화 기록 토글은 맨 마지막에 붙입니다.

    속성·표는 빈 칸만 채우므로 다시 돌아도 같은 결과지만, 토글은 붙일 때마다 하나씩 늘어납니다.
    앞 단계에서 실패하면 토글 없이 멈추고, 📝 가 없으니 다음 회차에 처음부터 다시 합니다.
    """
    if property_updates:
        notion.pages.update(page_id=page_id, properties=property_updates)
    for row_id, cells in row_updates:
        notion.blocks.update(block_id=row_id, table_row={"cells": cells})
    if after:
        notion.blocks.children.append(page_id, children=[log_block], after=after)
    else:
        notion.blocks.children.append(page_id, children=[log_block])


async def extract_consult(comment_text: str, comment_date: date) -> ConsultExtract:
    """상담 댓글에서 노션에 쓸 값을 뽑습니다."""
    llm = ChatOpenAI(model=DEFAULT_MODEL).with_structured_output(ConsultExtract)
    return await llm.ainvoke(
        [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": extract_prompt(comment_text, comment_date)},
        ]
    )


async def record_consultation(
    client: AsyncWebClient,
    root: dict,
    comments: list[dict],
    notion_url: str,
) -> None:
    """상담 댓글 묶음 하나를 노션에 옮기고 스레드에 결과를 답합니다."""
    last = comments[-1]
    text = "\n\n".join(c["text"] for c in comments)
    call_date = ts_to_datetime(last["ts"]).date()
    extract = await extract_consult(text, call_date)

    if not extract.is_consultation:
        for comment in comments:
            await client.reactions_add(
                channel=CHANNEL_ID, timestamp=comment["ts"], name=SKIPPED_REACTION
            )
        print(f"[hackathon_inquiry] 상담 아님: {root['ts']}")
        return

    page_id = _page_id(notion_url)
    page = await asyncio.to_thread(_read_consult_page, page_id)
    number = call_log_number(page.layout.call_log_titles)
    author = USER_NAMES[last["user"]]
    plan = plan_properties(page.properties, extract, call_date)
    row_updates, filled, empty = plan_essentials(page.essential_rows, extract)
    log_block = call_log_block(
        number,
        call_date,
        author,
        text,
        extract,
        f"{permalink(last['ts'])}?thread_ts={root['ts']}&cid={CHANNEL_ID}",
    )
    await asyncio.to_thread(
        _write_consult_page,
        page_id,
        page.layout.call_log_after,
        log_block,
        plan.updates,
        row_updates,
    )

    for comment in comments:
        await client.reactions_add(
            channel=CHANNEL_ID, timestamp=comment["ts"], name=RECORDED_REACTION
        )
    reply = format_record_reply(
        SUMMARY_MARKER, notion_url, number, call_date, author, plan, filled, empty
    )
    await client.chat_postMessage(
        channel=CHANNEL_ID, thread_ts=root["ts"], text=reply, unfurl_links=False
    )
    print(f"[hackathon_inquiry] 상담 기록: {notion_url} 통화 {number}")


async def sync_consult_comments(client: AsyncWebClient) -> None:
    """담당자의 상담 댓글을 노션 상담 페이지로 옮깁니다 (F5). 스케줄러가 2분마다 부릅니다."""
    now = datetime.now(KST)
    me = await bot_user_id(client)
    for message in await _root_messages(client, now - CONSULT_WINDOW):
        latest = message.get("latest_reply")
        if not latest or _consult_scanned.get(message["ts"]) == latest:
            continue
        thread = await _thread(client, message["ts"])
        summaries = [m for m in thread if _is_summary(m)]
        if not summaries:
            _consult_scanned[message["ts"]] = latest
            continue
        notion_url = summary_notion_url(summaries[0]["text"])
        comments = pending_comments(thread, summaries[0]["ts"], CONSULT_AUTHORS, me)
        if notion_url is None or not comments:
            _consult_scanned[message["ts"]] = latest
            continue
        if now - ts_to_datetime(comments[-1]["ts"]) < CONSULT_SETTLE:
            continue
        await record_consultation(client, message, comments, notion_url)


# ---- 로봇 도구 ----


def get_hackathon_tools(
    client: AsyncWebClient, channel: str, thread_ts: str, user_id: str | None
) -> list[StructuredTool]:
    """해커톤 문의 도구. 문의 채널에서는 문의 등록과 점검, 담당자 DM 에서는 점검만.

    Args:
        client: 대표 봇 클라이언트
        channel: 대화 중인 채널
        thread_ts: 대화 중인 스레드
        user_id: 질문자

    Returns:
        list[StructuredTool]: 이 대화에 줄 도구. 해당 없으면 빈 목록
    """

    @tool
    async def check_hackathon_leads() -> str:
        """
        해커톤 리드 파이프라인(노션)을 점검합니다. 오늘 연락할 리드와 자료가 빠진 리드를
        정리해 돌려줍니다. "점검", "오늘 연락할 리드", "빠진 자료" 같은 요청에 씁니다.
        돌려받은 글을 고치지 말고 그대로 답하세요.

        Returns:
            점검 결과. 해당이 없으면 그렇다고 알려 줍니다.
        """
        report = await asyncio.to_thread(
            build_morning_report, notion, datetime.now(KST).date()
        )
        return (
            report.text
            if report
            else "오늘 연락할 리드도, 자료가 빠진 리드도 없습니다."
        )

    @tool
    async def register_hackathon_inquiry(
        org: str,
        contact: str = "",
        phone: str = "",
        email: str = "",
        target: str = "",
        scale: str = "",
        schedule: str = "",
        body: str = "",
    ) -> str:
        """
        사람이 이 스레드에 붙인 해커톤 문의(메일 전달 등)를 접수합니다. 폼으로 들어온
        문의처럼 스레드에 정리 글을 달고 담당자를 멘션하며 노션 리드에 등록합니다.
        "문의 등록" 요청에만 씁니다. 값은 원글에 적힌 것만 넣고 지어내지 않습니다.

        Args:
            org: 기관·학교명
            contact: 기관 담당자 이름·직함
            phone: 연락처
            email: 이메일
            target: 대상. 학교 / 대학 / 기업 / 기관·지자체 중 하나
            scale: 규모 원문 (예: "30명", "미정")
            schedule: 희망 시기 원문
            body: 문의 내용 전문

        Returns:
            스레드에 단 정리 글
        """
        fields = {
            "기관명": org,
            "담당자": contact,
            "연락처": phone,
            "이메일": email,
            "대상": target,
            "규모": scale,
            "희망 시기": schedule,
            "문의 내용": body,
        }
        submission = Submission(
            title="해커톤 문의 새 제출",
            fields={label: value for label, value in fields.items() if value},
            admin_url=None,
        )
        thread = await _thread(client, thread_ts)
        if any(_is_summary(m) for m in thread):
            return "이 스레드는 이미 접수했습니다. 정리 글을 확인하세요."
        return await process_submission(client, submission, thread_ts, "인바운드-메일")

    if channel == CHANNEL_ID:
        return [register_hackathon_inquiry, check_hackathon_leads]
    if channel.startswith("D") and user_id in CONSULT_AUTHORS:
        return [check_hackathon_leads]
    return []
