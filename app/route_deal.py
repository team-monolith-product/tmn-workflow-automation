"""
코들 문의 신청 폼 제출 메시지를 AI로 분석하여 Notion '딜' DB에 자동으로 항목을 생성합니다.

스레드 내용을 전부 읽고, Notion DB 스키마와 샘플 데이터를 AI에 전달하여
속성값을 동적으로 결정합니다.
"""

import json
import os

from notion_client import Client as NotionClient
from openai import OpenAI
from slack_sdk.web.async_client import AsyncWebClient

DEAL_DATA_SOURCE_ID = "3221cc82-0da6-8059-b2a3-000bbc7eb6b5"

# AI가 채울 수 있는 속성 타입
FILLABLE_TYPES = {
    "title",
    "number",
    "select",
    "multi_select",
    "status",
    "date",
    "rich_text",
    "email",
}


def get_fillable_properties(data_source: dict) -> dict:
    """DB 스키마에서 AI가 채울 수 있는 속성만 추출합니다.

    제외 대상:
    - 이름에 '추후제거'가 포함된 deprecated 속성
    - people, files, formula, rollup, relation 등 AI가 직접 채울 수 없는 타입
    """
    properties = data_source.get("properties", {})
    fillable = {}
    for name, prop in properties.items():
        if "추후제거" in name:
            continue
        prop_type = prop.get("type", "")
        if prop_type in FILLABLE_TYPES:
            fillable[name] = prop
    return fillable


def build_schema_description(fillable_props: dict) -> str:
    """AI에게 전달할 스키마 설명을 생성합니다."""
    lines = []
    for name, prop in fillable_props.items():
        prop_type = prop["type"]
        desc = f"- {name} ({prop_type})"

        if prop_type == "select":
            options = [o["name"] for o in prop.get("select", {}).get("options", [])]
            if options:
                desc += f": 가능한 값 = [{', '.join(options)}]"
        elif prop_type == "multi_select":
            options = [
                o["name"] for o in prop.get("multi_select", {}).get("options", [])
            ]
            if options:
                desc += f": 가능한 값 = [{', '.join(options)}]"
        elif prop_type == "status":
            options = [o["name"] for o in prop.get("status", {}).get("options", [])]
            if options:
                desc += f": 가능한 값 = [{', '.join(options)}]"
        elif prop_type == "number":
            fmt = prop.get("number", {}).get("format", "")
            if fmt:
                desc += f" (format: {fmt})"

        lines.append(desc)
    return "\n".join(lines)


def format_sample_rows(rows: list[dict], fillable_props: dict) -> str:
    """샘플 행의 주요 속성값을 포맷팅합니다."""
    samples = []
    for i, row in enumerate(rows, 1):
        props = row.get("properties", {})
        row_data = {}
        for name in fillable_props:
            if name not in props:
                continue
            val = _extract_property_value(props[name])
            if val is not None and val != "" and val != []:
                row_data[name] = val

        if row_data:
            samples.append(f"예시 {i}: {json.dumps(row_data, ensure_ascii=False)}")
    return "\n".join(samples)


def _extract_property_value(prop: dict):
    """Notion 속성에서 표시용 값을 추출합니다."""
    prop_type = prop.get("type", "")
    if prop_type == "title":
        texts = prop.get("title", [])
        return texts[0].get("plain_text", "") if texts else None
    elif prop_type == "number":
        return prop.get("number")
    elif prop_type == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else None
    elif prop_type == "multi_select":
        ms = prop.get("multi_select", [])
        return [m.get("name", "") for m in ms] if ms else []
    elif prop_type == "status":
        st = prop.get("status")
        return st.get("name", "") if st else None
    elif prop_type == "rich_text":
        texts = prop.get("rich_text", [])
        return texts[0].get("plain_text", "") if texts else None
    elif prop_type == "email":
        return prop.get("email")
    elif prop_type == "date":
        dt = prop.get("date")
        return dt.get("start") if dt else None
    return None


def build_ai_json_schema(fillable_props: dict) -> dict:
    """OpenAI structured output용 JSON 스키마를 동적으로 생성합니다."""
    schema_properties = {}

    for name, prop in fillable_props.items():
        prop_type = prop["type"]

        if prop_type in ("title", "rich_text", "email", "date"):
            schema_properties[name] = {
                "anyOf": [{"type": "string"}, {"type": "null"}],
            }
        elif prop_type == "number":
            schema_properties[name] = {
                "anyOf": [{"type": "number"}, {"type": "null"}],
            }
        elif prop_type == "select":
            options = [o["name"] for o in prop.get("select", {}).get("options", [])]
            if options:
                schema_properties[name] = {
                    "anyOf": [{"type": "string", "enum": options}, {"type": "null"}],
                }
            else:
                schema_properties[name] = {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                }
        elif prop_type == "multi_select":
            options = [
                o["name"] for o in prop.get("multi_select", {}).get("options", [])
            ]
            if options:
                schema_properties[name] = {
                    "type": "array",
                    "items": {"type": "string", "enum": options},
                }
            else:
                schema_properties[name] = {
                    "type": "array",
                    "items": {"type": "string"},
                }
        elif prop_type == "status":
            options = [o["name"] for o in prop.get("status", {}).get("options", [])]
            if options:
                schema_properties[name] = {
                    "anyOf": [{"type": "string", "enum": options}, {"type": "null"}],
                }
            else:
                schema_properties[name] = {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                }

    return {
        "type": "object",
        "properties": schema_properties,
        "required": list(schema_properties.keys()),
        "additionalProperties": False,
    }


def analyze_thread_for_deal(
    thread_messages: list[str],
    fillable_props: dict,
    schema_description: str,
    sample_rows_text: str,
) -> dict:
    """스레드 내용을 AI로 분석하여 Notion 딜 속성값을 결정합니다."""
    client = OpenAI()

    thread_text = "\n---\n".join(thread_messages)
    json_schema = build_ai_json_schema(fillable_props)

    system_prompt = f"""당신은 슬랙 메시지에서 고객 문의 정보를 추출하여 Notion 데이터베이스에 기록하는 전문가입니다.

아래는 대상 Notion 데이터베이스의 속성(컬럼) 목록입니다:
{schema_description}

아래는 기존 데이터베이스의 샘플 데이터입니다 (형식 참고용):
{sample_rows_text}

규칙:
1. 스레드 내용을 분석하여 각 속성에 적합한 값을 결정하세요.
2. 확실하게 판단할 수 있는 속성만 채우세요. 판단할 수 없으면 null (또는 빈 배열)로 두세요.
3. select/multi_select/status 속성은 반드시 제공된 옵션 목록에서만 선택하세요.
4. 샘플 데이터의 형식과 패턴을 참고하여 일관된 형식으로 입력하세요.
5. 단계(status)는 새 문의이므로 "홈페이지견적요청"으로 설정하세요.
6. Name(title)은 "학교명 - 성함" 형식으로 작성하세요 (샘플 데이터 참고).
7. 숫자 값은 쉼표 없이 순수 숫자로 입력하세요."""

    response = client.responses.create(
        model="gpt-4o",
        input=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"아래 슬랙 스레드 내용을 분석하여 Notion 딜 속성값을 결정해주세요:\n\n{thread_text}",
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "deal_properties",
                "schema": json_schema,
                "strict": True,
            }
        },
    )

    return json.loads(response.output_text)


def build_notion_properties(
    ai_result: dict,
    fillable_props: dict,
    bot_user_id: str,
) -> dict:
    """AI 결과를 Notion API 형식의 properties로 변환합니다."""
    properties = {}

    for name, value in ai_result.items():
        if value is None:
            continue
        if name not in fillable_props:
            continue

        prop = fillable_props[name]
        prop_type = prop["type"]

        if prop_type == "title" and value:
            properties[name] = {"title": [{"text": {"content": str(value)}}]}
        elif prop_type == "number" and isinstance(value, (int, float)):
            properties[name] = {"number": value}
        elif prop_type == "select" and value:
            properties[name] = {"select": {"name": str(value)}}
        elif prop_type == "multi_select" and isinstance(value, list) and value:
            properties[name] = {"multi_select": [{"name": str(v)} for v in value]}
        elif prop_type == "status" and value:
            properties[name] = {"status": {"name": str(value)}}
        elif prop_type == "rich_text" and value:
            properties[name] = {"rich_text": [{"text": {"content": str(value)}}]}
        elif prop_type == "email" and value:
            properties[name] = {"email": str(value)}
        elif prop_type == "date" and value:
            properties[name] = {"date": {"start": str(value)}}

    # 담당자는 항상 봇 사용자로 설정
    properties["담당자"] = {"people": [{"id": bot_user_id}]}

    return properties


async def route_deal(
    slack_client: AsyncWebClient,
    body: dict,
) -> None:
    """코들 문의 신청 메시지를 AI로 분석하여 Notion 딜 DB에 항목을 생성합니다."""
    event = body.get("event", {})
    message_text = event.get("text", "")
    channel_id = event.get("channel")
    message_ts = event.get("ts")

    # 1. 스레드의 모든 메시지 수집
    thread_messages = [message_text]
    try:
        thread_response = await slack_client.conversations_replies(
            channel=channel_id,
            ts=message_ts,
        )
        thread_messages = [
            msg.get("text", "")
            for msg in thread_response.get("messages", [])
            if msg.get("text")
        ]
    except Exception:
        pass  # 스레드가 없으면 원본 메시지만 사용

    # 2. Notion DB 스키마 조회
    notion = NotionClient(auth=os.environ.get("NOTION_TOKEN"))
    data_source = notion.data_sources.retrieve(DEAL_DATA_SOURCE_ID)
    fillable_props = get_fillable_properties(data_source)
    schema_description = build_schema_description(fillable_props)

    # 3. 샘플 데이터 2~3행 조회
    sample_response = notion.data_sources.query(
        data_source_id=DEAL_DATA_SOURCE_ID,
        page_size=3,
    )
    sample_rows_text = format_sample_rows(
        sample_response.get("results", []),
        fillable_props,
    )

    # 4. AI로 속성값 결정
    ai_result = analyze_thread_for_deal(
        thread_messages, fillable_props, schema_description, sample_rows_text
    )
    print(f"route_deal: AI 분석 결과: {json.dumps(ai_result, ensure_ascii=False)}")

    # 5. Notion 페이지 생성
    bot_user = notion.users.me()
    properties = build_notion_properties(ai_result, fillable_props, bot_user["id"])

    response = notion.pages.create(
        parent={"data_source_id": DEAL_DATA_SOURCE_ID},
        properties=properties,
    )
    page_url = response["url"]
    page_id = response["id"]

    # 6. 본문에 슬랙 메시지 북마크 추가
    slack_message_url = (
        f"https://monolith-keb2010.slack.com"
        f"/archives/{channel_id}/p{message_ts.replace('.', '')}"
    )
    notion.blocks.children.append(
        block_id=page_id,
        children=[{"type": "bookmark", "bookmark": {"url": slack_message_url}}],
    )

    # 7. 스레드에 확인 메시지
    title = ai_result.get("Name") or "(제목 없음)"
    filled_fields = [k for k, v in ai_result.items() if v is not None and k != "Name"]
    filled_summary = ", ".join(filled_fields) if filled_fields else "없음"

    await slack_client.chat_postMessage(
        channel=channel_id,
        text=(
            f"Notion 딜에 등록했습니다.\n"
            f"• 이름: {title}\n"
            f"• 채워진 항목: {filled_summary}\n"
            f"• <{page_url}|Notion에서 보기>"
        ),
        thread_ts=message_ts,
    )
    print(f"route_deal: Notion 딜 생성 완료. title={title}, url={page_url}")
