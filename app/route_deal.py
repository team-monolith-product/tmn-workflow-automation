"""
코들 문의 신청 폼 제출 메시지를 파싱하여 Notion '딜' DB에 자동으로 항목을 생성합니다.

슬랙 채널에 workflow-automation 봇이 게시하는 폼 제출 메시지를 감지하여
파싱 후 Notion 딜 데이터베이스에 새 항목을 만들고, 스레드에 확인 메시지를 남깁니다.
"""

import os
import re

from notion_client import Client as NotionClient
from slack_sdk.web.async_client import AsyncWebClient

DEAL_DATA_SOURCE_ID = "3221cc82-0da6-8059-b2a3-000bbc7eb6b5"

# 플랜명 → Notion 모델 multi_select 옵션
PLAN_NAME_TO_MODEL: dict[str, str] = {
    "Pro 플랜": "Pro",
    "AI 플랜": "AI",
    "씨마스, 비상 플랜": "씨마스/비상",
    "찾아가는 AI 체험학교 패키지": "AI체험학교",
    "바이브코딩 해커톤 패키지": "해커톤",
}

KNOWN_PLAN_NAMES: list[str] = list(PLAN_NAME_TO_MODEL.keys())


def parse_form_submission(text: str) -> dict | None:
    """코들 문의 신청 슬랙 메시지를 파싱하여 딕셔너리로 반환합니다.

    Args:
        text: 슬랙 메시지 텍스트 (mrkdwn 형식)

    Returns:
        파싱된 데이터 딕셔너리 또는 코들 문의 메시지가 아니면 None
    """
    if "코들 문의 신청" not in text:
        return None

    # 어드민 링크 부분을 먼저 분리 (마지막 필드 값에 붙어있을 수 있음)
    text_clean = re.sub(r"\s*어드민 링크:.*$", "", text)

    # *field_name*\nvalue 패턴 추출
    # [^\n*]+ 으로 value가 다음 필드 마커(*) 또는 개행 전까지만 캡처
    fields: dict[str, str] = {}
    for match in re.finditer(r"\*([^*]+)\*\n([^\n*]+)", text_clean):
        key = match.group(1).strip()
        value = match.group(2).strip()
        # Slack tel: 링크 정리: <tel:xxx|yyy> → yyy
        value = re.sub(r"<tel:[^|]+\|([^>]+)>", r"\1", value)
        fields[key] = value

    school = fields.get("학교명", "")
    name = fields.get("성함", "")
    if not school or not name:
        return None

    # 플랜 정보 추출
    plans: list[dict] = []
    for plan_name in KNOWN_PLAN_NAMES:
        students_str = fields.get(f"{plan_name} (학생 수)")
        if not students_str:
            continue
        price_str = fields.get(f"{plan_name} (가격)", "0") or "0"
        plans.append(
            {
                "name": plan_name,
                "students": int(students_str),
                "semesters": fields.get(f"{plan_name} (사용 학기 수)", ""),
                "price": int(price_str),
            }
        )

    # 어드민 링크 추출
    admin_link_match = re.search(
        r"<(https://admin\.codle\.io/form_submissions/\d+/show)>", text
    )

    return {
        "school": school,
        "name": name,
        "phone": fields.get("휴대전화번호", ""),
        "source": fields.get("코들을 알게 된 경로", ""),
        "submission_time": fields.get("제출 시각", ""),
        "plans": plans,
        "total_students": sum(p["students"] for p in plans),
        "total_price": sum(p["price"] for p in plans),
        "admin_link": admin_link_match.group(1) if admin_link_match else None,
    }


async def route_deal(
    slack_client: AsyncWebClient,
    body: dict,
) -> None:
    """코들 문의 신청 메시지를 파싱하여 Notion 딜 DB에 항목을 생성합니다."""
    event = body.get("event", {})
    message_text = event.get("text", "")
    channel_id = event.get("channel")
    message_ts = event.get("ts")

    data = parse_form_submission(message_text)
    if data is None:
        print(f"route_deal: 파싱 실패 또는 코들 문의 아님. text={message_text[:100]}")
        return

    notion = NotionClient(auth=os.environ.get("NOTION_TOKEN"))

    # 담당자: 팀모노리스 로봇 호출자 (= Notion API 봇)
    bot_user = notion.users.me()
    bot_user_id = bot_user["id"]

    # Notion 모델 multi_select
    models = [
        {"name": PLAN_NAME_TO_MODEL[p["name"]]}
        for p in data["plans"]
        if p["name"] in PLAN_NAME_TO_MODEL
    ]

    # 페이지 속성 구성
    title = f"{data['school']} - {data['name']}"
    properties: dict = {
        "Name": {"title": [{"text": {"content": title}}]},
        "단계": {"status": {"name": "홈페이지견적요청"}},
        "담당자": {"people": [{"id": bot_user_id}]},
    }
    if models:
        properties["모델"] = {"multi_select": models}
    if data["total_students"] > 0:
        properties["인원"] = {"number": data["total_students"]}
    if data["total_price"] > 0:
        properties["견적금액"] = {"number": data["total_price"]}

    # 페이지 생성
    response = notion.pages.create(
        parent={"data_source_id": DEAL_DATA_SOURCE_ID},
        properties=properties,
    )
    page_url = response["url"]
    page_id = response["id"]

    # 본문: 슬랙 링크 + 어드민 링크 + 상세 정보
    slack_message_url = (
        f"https://monolith-keb2010.slack.com"
        f"/archives/{channel_id}/p{message_ts.replace('.', '')}"
    )
    children: list[dict] = [
        {"type": "bookmark", "bookmark": {"url": slack_message_url}},
    ]
    if data["admin_link"]:
        children.append(
            {"type": "bookmark", "bookmark": {"url": data["admin_link"]}}
        )

    detail_lines = []
    if data["phone"]:
        detail_lines.append(f"휴대전화번호: {data['phone']}")
    if data["source"]:
        detail_lines.append(f"코들을 알게 된 경로: {data['source']}")
    for plan in data["plans"]:
        line = f"{plan['name']}: 학생 {plan['students']}명"
        if plan["semesters"]:
            line += f", 학기 {plan['semesters']}"
        if plan["price"]:
            line += f", 가격 {plan['price']:,}원"
        detail_lines.append(line)

    for line in detail_lines:
        children.append(
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": line}}]
                },
            }
        )

    notion.blocks.children.append(block_id=page_id, children=children)

    # 스레드에 확인 메시지
    plan_summary = ", ".join(
        PLAN_NAME_TO_MODEL.get(p["name"], p["name"]) for p in data["plans"]
    )
    await slack_client.chat_postMessage(
        channel=channel_id,
        text=(
            f"Notion 딜에 등록했습니다.\n"
            f"• 이름: {title}\n"
            f"• 모델: {plan_summary}\n"
            f"• 인원: {data['total_students']}명\n"
            f"• 견적금액: {data['total_price']:,}원\n"
            f"• <{page_url}|Notion에서 보기>"
        ),
        thread_ts=message_ts,
    )
    print(
        f"route_deal: Notion 딜 생성 완료. title={title}, url={page_url}"
    )
