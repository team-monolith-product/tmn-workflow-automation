import os
import re
from typing import List, Dict, Any

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from notion2md.exporter.block import StringExporter
from notion2md.convertor.block import BLOCK_TYPES
from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from md2notionpage.core import parse_md

# 환경 변수 로드
load_dotenv()

AIDT_UPDATE_DATABASE = '15a1cc820da68092af44fa0d2975cba4'

def main():
    """
    메인 함수:
    1) 노션, 슬랙, OpenAI 클라이언트 생성
    2) 특정 노션 페이지(또는 데이터베이스)를 조회하여 관계된 '작업' 페이지들(선행 작업 포함) 정보를 변환
    3) 본문 내 슬랙 링크를 찾아서 슬랙 대화 내용을 가져옴
    4) 최종적으로 관련 문서(수정 보완 내역, 작업 내역서, 기획서)와 슬랙 대화 기록을 포함하는 프롬프트 생성
    """
    notion = NotionClient(auth=os.environ.get("NOTION_TOKEN"))
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
    openai = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    # 1) 데이터베이스에서 '회의록'이 없는 페이지를 찾아서 처리
    response = notion.databases.query(
        database_id=AIDT_UPDATE_DATABASE,
        filter={
            "property": "회의록",
            "relation": {
                "is_empty": True
            }
        }
    )

    for root_page_id in [page["id"] for page in response["results"]]:
        # 2) transform_notion_page: 해당 페이지 + Relation("작업") 속의 페이지를 재귀적으로 수집
        related_pages = transform_notion_page(notion, root_page_id)

        # 3) body(마크다운)에서 슬랙 링크 추출 -> 실제 슬랙 대화 가져옴
        #    transform_notion_page 함수 결과 중 "slack_conversations" 필드를 종합
        slack_conversations = []
        for page in related_pages:
            for conversation_info in page["slack_conversations"]:
                channel_id = conversation_info.get("channel")
                thread_ts = conversation_info.get("ts")
                if channel_id and thread_ts:
                    slack_conversations.append(
                        transform_slack_conversation(
                            slack_client, channel_id, thread_ts)
                    )

        print("=== 관련 페이지 정보 ===")
        for page in related_pages:
            print(page)

        print("=== 슬랙 대화 정보 ===")
        for conv in slack_conversations:
            print(conv)

        # 4) 회의록 등에 활용할 최종 프롬프트 생성
        final_prompt = generate_prompt(related_pages, slack_conversations)

        print("=== 최종 프롬프트 ===")
        print(final_prompt)

        # 5) 필요하다면 OpenAI API 등을 이용해 실제 회의록 등 요약을 진행할 수 있음.
        #    아래는 예시 형태로 두었으며, 실제로 사용 시 마무리 구현 필요.
        meeting_minutes = generate_meeting_minutes(openai, final_prompt)
        print("회의록 초안:\n", meeting_minutes)

        confirm = input("회의록 초안을 생성하시겠습니까? (y/n): ")
        
        if confirm.lower() != "y":
            print("생성을 취소합니다.")
            continue

        # 6) OpenAI API를 통해 생성된 회의록 초안을 Notion 페이지에 업데이트
        create_meeting_minutes_page(notion, root_page_id, meeting_minutes)

def transform_notion_page(
    notion: NotionClient,
    page_id: str,
    visited=None
) -> List[Dict[str, Any]]:
    """
    주어진 Notion 페이지 ID에 대해:
    1) 제목, 본문, 타임라인, 슬랙 링크 등을 파싱
    2) '선행 작업' 혹은 '작업' Relation 필드를 재귀적으로 추적하여 그 페이지들도 수집
    3) 각 페이지에 대한 정보를 다음 형태의 dict 리스트로 반환:

    [
        {
            "title": "판서기능 추가",
            "body": "#1. 수정 내용 키워드\n...",
            "timeline": {
                "start": "2024-10-01",
                "end": "2024-10-02",
            },
            "slack_conversations": [
                {
                    "channel": "C123456",
                    "ts": "1234567890.123456",
                },
                ...
            ],
        },
        ... (재귀적으로 수집)
    ]

    주의: 실제 DB의 프로퍼티 이름, Relation 속성 이름, Date 속성 이름을 본인 환경에 맞춰 수정 필요.
    """
    if visited is None:
        visited = set()

    # 이미 방문한 페이지는 중복 수집 방지
    if page_id in visited:
        return []
    visited.add(page_id)

    try:
        page = notion.pages.retrieve(page_id=page_id)
    except Exception as e:
        print(f"[transform_notion_page] 페이지 조회 오류: {e}")
        return []

    # 제목(Property: "Name") 가져오기 (Notion 기본 property명은 "title" or "Name" 등)
    title = extract_page_title(page)

    # 본문(Markdown) 가져오기
    body_md = get_notion_markdown_body(page_id)

    slack_conversations = parse_slack_links_from_body(body_md)

    # "타임라인"이라는 Date Range 프로퍼티가 있다고 가정
    # (실제 DB에서는 프로퍼티명이 다를 수 있음)
    timeline = extract_timeline_property(page)

    # "선행 작업" or "작업"이라는 Relation 속성 이름이 있다고 가정
    related_pages_info = page["properties"].get("작업")
    if not related_pages_info:
        related_pages_info = page["properties"].get("선행 작업")

    # 반환할 데이터 구조
    current_page_data = {
        "title": title,
        "body": body_md,
        "timeline": timeline,
        "slack_conversations": slack_conversations,
    }

    results = [current_page_data]

    # Relation으로 연결된 페이지들을 재귀 탐색
    if related_pages_info and related_pages_info["type"] == "relation":
        for related_item in related_pages_info["relation"]:
            child_page_id = related_item["id"]
            results.extend(transform_notion_page(
                notion, child_page_id, visited=visited))

    return results


def transform_slack_conversation(
    slack_client: WebClient,
    channel_id: str,
    ts: str
) -> List[Dict[str, Any]]:
    """
    Slack 대화를 불러와서 다음과 같은 형태의 리스트를 반환한다고 가정:
    [
        {
            "user": "U123456",
            "text": "안녕하세요",
            "ts": "12345.67890"
        },
        ...
    ]
    thread_ts=ts 기준으로 Replies를 가져올 수도 있고, 그냥 해당 메시지만 가져올 수도 있음.

    실제로는 slack_client.conversations_replies 등을 사용.
    """
    try:
        response = slack_client.conversations_replies(
            channel=channel_id,
            ts=ts
        )
        messages = response.get("messages", [])
    except SlackApiError as e:
        print(f"[transform_slack_conversation] 슬랙 API 에러: {e}")
        return []

    results = []
    for msg in messages:
        user = msg.get("user", "")
        text = msg.get("text", "")
        ts_ = msg.get("ts", "")
        results.append({
            "user": user,
            "text": text,
            "ts": ts_
        })
    return results


def get_notion_markdown_body(
    page_id: str,
) -> str:
    """
    노션 페이지 본문을 마크다운 형태로 조회합니다.
    notion2md의 StringExporter를 사용.
    """
    try:
        md_text = StringExporter(
            block_id=page_id, output_path="dummy").export()
        return md_text
    except Exception as e:
        print(f"[get_notion_markdown_body] 노션 본문 변환 오류: {e}")
        return ""


def generate_meeting_minutes(
    openai: OpenAI,
    prompt: str
) -> str:
    """
    예시로, prompt를 입력하여 OpenAI API의 ChatCompletion 등을 통해
    회의록 초안을 생성한다고 가정. (실제 구현은 개발 환경에 맞게)
    """
    response = openai.chat.completions.create(
        model="o1",
        messages=[
            {
                "role": "system",
                "content": """Context:
- AI 디지털 교과서는 대한민국에서 25년에 도입될 신규 교육과정 및 시스템.
- 본사는 AI 디지털 교과서 중 고등학교 정보 과목의 개발 및 운영을 담당.
- 시스템의 수정을 위해 특정 양식을 갖춘 '회의록'을 작성하고 교육부에 공유함.
- 사내에서 노션으로 기획서, 과업 지시서, 작업 내역서 등을 보유하고 있음.
- 문서 내부에 슬랙 링크가 연결되어 있기도 함. 해당 슬랙 링크는 일종의 회의록 원본으로 취급될 수 있음.
- 코들은 AI 디지털 교과서의 전신인 자사 서비스임. 본사는 코들을 교육부의 요구사항에 맞게 수정하여 AI 디지털 교과서를 개발함.
  25년 교과서 도입전까지 교사들이 보조 도구로 코들을 학교 예산으로 구매하여 사용하고 있음.
- VF는 Viewable Framework의 약자로 학생이 학습 자료를 보고 상호 작용하는 환경을 의미함.
- EF는 Editable Framework의 약자로 교사가 학습 자료를 수정하는 환경을 의미함.
- OF는 Observable Framework의 약자로 교사가 학생의 학습 상황을 관찰하는 환경을 의미함.
- CDS는 Codle Design System의 약자.
- SNB: Side Navigation Bar, GNB: Global Navigation Bar, LNB: Local Navigation Bar
- AIDT: AI Digital Textbook, AI 디지털 교과서
- 민간: 코들 서비스를 의미. 반대) AIDT. AI 디지털 교과서를 의미.
- 사용 불편을 보고하는 주체는 주로 사내 직원, 교사, 출판사(저자)이며 교육부가 아니다.

Instruction:
- 주어진 각종 문서와 대화 기록을 바탕으로 회의록을 작성한다.
- 이 회의록은 외부(교육부)에 공유되므로 형식을 준수하고 공적인 문서로 작성한다.
  
- 회의록은 아래 양식을 따른다.
  - 일시: XXXX년 MM월 DD일. 주로 함께 전달된 문서의 일시를 참고하여 결정.
  - 참여자:
    - 익명으로 표현하여 직책으로만 표기
    - 기획자, 개발자, 디자이너의 직책 사용.
  - 배경
  - 문제점
  - 참여자 발언 요약
  - 결론 및 액션 플랜
- Example 예시의 규격을 강력히 준수하며 Numbered List는 사용하지 않는다.

Example:
### 일시
- 2024년 12월 26일

### 참여자
- 기획자
- 개발자
- 디자이너

### 배경
- AI 디지털 교과서 내 PDF 노트 기능에서 한 줄로 긴 문장을 작성할 경우 줄바꿈(랩핑)이 적용되지 않아 일부 내용이 가려지는 문제가 제기됨  
- 기존에도 글자 수 제한(255자) 방안이 마련되었으나 긴 문장을 작성하는 사용자의 경우 불편을 겪고 있음

### 문제점
- PDF 노트에 긴 문장을 입력하면 화면 너비를 넘어서는 부분이 표시되지 않아 사용자 경험이 저하됨
- 디자인 측면에서 줄바꿈 적용 시 레이아웃이나 UI 요소가 어긋날 수 있는 가능성 존재

### 참여자 발언 요약
- 기획자: "내용이 길어지면 줄바꿈이 제대로 이루어지지 않아 학습자가 노트 내용을 확인하기 어렵습니다. 사용성이 떨어지지 않도록 보완이 필요합니다."  
- 개발자: "줄바꿈 랩핑 기능을 적용해 문제를 해결할 수 있으며, UI 충돌이 있는지 반드시 확인해야 합니다."  
- 디자이너: "UI 구조상 길이가 늘어났을 때 디자인적 측면에서 여백이나 정렬 이슈가 없는지 점검 후, 노트 화면에서 자연스럽게 줄바꿈이 이루어지도록 작업하겠습니다."

### 결론 및 액션 플랜
- 노트 작성 시 화면 너비를 초과하는 경우 자동 줄바꿈 기능 적용  
- UI 디자인 체크리스트 수립 후 실제 적용 시 문제가 없는지 검증"""
            },
            {
                "role": "user",
                "content": prompt
            },
        ]
    )
    return response.choices[0].message.content


def generate_prompt(
    related_pages: List[Dict[str, Any]],
    slack_conversations: List[List[Dict[str, Any]]],
) -> str:
    """
    다음 형식을 만족하는 프롬프트를 생성한다.
    <문서1: {Title}>
    </문서1: {Title}>

    <문서2: {Title}>
    </문서2: {Title}>

    ...

    <대화1>
    </대화1>

    <대화2>
    </대화2>

    ...
    """
    prompt = []

    for i, page in enumerate(related_pages):
        page = related_pages[i]
        body = page["body"]

        prompt.append(f"<문서{i+1}: {page['title']}>")

        timeline = page["timeline"]
        if timeline:
            start = timeline.get("start", "")
            end = timeline.get("end", "")
            prompt.append(f"**일시**: {start} ~ {end}")

        prompt.append(body)
        prompt.append(f"</문서{i+1}: {page['title']}>\n")

    for i, conv in enumerate(slack_conversations):
        prompt.append(f"<대화{i+1}>")
        for msg in conv:
            user = msg.get("user", "")
            text = msg.get("text", "")
            ts = msg.get("ts", "")
            prompt.append(f"- ({ts}) {user}: {text}")
        prompt.append(f"</대화{i+1}>\n")

    return "\n".join(prompt)


def create_meeting_minutes_page(
    notion: NotionClient,
    root_page_id: str,
    meeting_minutes: str
):
    """
    회의록 초안을 Notion 페이지에 업데이트한다.
    """
    root_page = notion.pages.retrieve(page_id=root_page_id)

    response = notion.pages.create(
        parent={"database_id": "1821cc820da68091b61dce23d7943eda"},
        properties={
            "제목": {
                "title": [
                    {
                        "text": {
                            "content": root_page["properties"]["제목"]["title"][0]["text"]["content"] + " 회의록"
                        }
                    }
                ]
            }
        }
    )

    notion.blocks.children.append(
        response["id"],
        children=parse_md(meeting_minutes)
    )

    notion.pages.update(
        page_id=root_page_id,
        properties={
            "회의록": {
                "relation": [
                    {
                        "id": response["id"]
                    }
                ]
            }
        }
    )

# --------------------------
# 아래는 보조/유틸 함수들
# --------------------------


def extract_page_title(page_data: Dict[str, Any]) -> str:
    """
    노션 API로 가져온 page 데이터에서 제목을 추출.
    노션의 title property(예: "Name" 또는 "title")를 찾아 반환.
    """
    # TODO: 실제 DB 상의 title property 이름 확인 필요
    properties = page_data.get("properties", {})
    title_prop = None

    # 일반적으로 "Title" 혹은 "Name"이라는 프로퍼티가 type=title 인 것을 찾음
    for prop_key, prop_val in properties.items():
        if prop_val.get("type") == "title":
            title_prop = prop_val
            break

    if not title_prop:
        return "제목 없음"

    # title_prop["title"]가 리스트 형태로 저장됨
    title_texts = title_prop["title"]
    text_val = "".join([t["plain_text"] for t in title_texts])
    return text_val.strip()


def extract_timeline_property(page_data: Dict[str, Any]) -> Dict[str, str]:
    """
    Notion의 date 프로퍼티(예: '타임라인')가 date range일 경우, start/end 추출
    반환 형식: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    """
    date_prop = page_data["properties"].get("타임라인")
    if not date_prop or date_prop.get("type") != "date":
        return {}

    date_info = date_prop.get("date", {})
    start = date_info.get("start")
    end = date_info.get("end")

    return {
        "start": start or "",
        "end": end or "",
    }


def parse_slack_links_from_body(md_text: str) -> List[Dict[str, str]]:
    """
    본문(Markdown) 내 슬랙 링크를 찾아,
    대화 조회에 필요한 channel, ts 정보를 추출한다.

    Slack 메시지 퍼머링크 예시:
    https://monolith-keb2010.slack.com/archives/C07A5HVG6UR/p1731572264871769

    - 위 URL에서 채널 ID는 C07A5HVG6UR
    - 메시지ID는 p 뒤에 붙은 1731572264871769
      이를 Slack API에서 통용되는 TS 형태(예: "1731572264.871769")로 변환한다.
    """
    # Slack permalink 정규식
    # https://<workspace>.slack.com/archives/<channel_id>/p<digits>
    pattern = r"https:\/\/[\w-]+\.slack\.com\/archives\/([A-Z0-9]+)/p(\d+)"

    matches = re.findall(pattern, md_text)
    conversations = []
    for channel, raw_ts in matches:
        # Slack permalink에서 p 뒤의 숫자는 예: 1731572264871769
        # 실제 Slack TS는 '1731572264.871769'처럼 중간에 '.'이 들어가야 한다.
        # 아래는 가장 일반적인 케이스: 앞 10자리가 초 단위 타임스탬프, 나머지가 소수점 이하
        if len(raw_ts) > 10:
            ts = raw_ts[:10] + "." + raw_ts[10:]
        else:
            # 혹시 숫자가 10자리 이하라면 그냥 그대로 사용
            ts = raw_ts

        conversations.append({
            "channel": channel,
            "ts": ts
        })
    return conversations


def link_preview(info: dict) -> str:
    return f"(Link Preview)[{info['url']}]"


BLOCK_TYPES['link_preview'] = link_preview

if __name__ == "__main__":
    main()
