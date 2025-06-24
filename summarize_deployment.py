"""
프로젝트 설명:
이 파이썬 스크립트는 오늘 배포 예정인 과업들을 노션(Notion) 데이터베이스에서 가져와, 지정된 슬랙(Slack) 채널에 포맷된 메시지를 전송합니다.
메시지에는 담당자가 멘션되고, 관련된 GitHub 풀 리퀘스트 링크가 포함됩니다.

요구 사항:
- 노션 API에 연결하여 '배포 예정 날짜'가 오늘인 과업들을 가져옵니다.
- 각 과업에 대해 다음을 추출합니다:
  - 정확한 매칭을 위해 담당자의 이메일을 추출하여 슬랙 사용자 ID와 매칭합니다.
  - 과업 제목.
  - 관련된 GitHub 풀 리퀘스트 링크들.
- 슬랙 API를 사용하여 다음과 같은 형식으로 특정 채널에 메시지를 전송합니다:

```
오늘 배포 예정 과업! @담당자 과업 제목 (PR 링크)
```

특이사항:
- 스크립트는 노션의 'people' 속성에 접근하여 담당자 이메일을 추출합니다.
- API 호출 제한을 주의하여 Bulk 요청을 활용합니다.

참고:
- 팀원들에게 오늘의 배포 과업에 대해 알리기 위한 워크플로우를 자동화하기 위해 개발되었습니다.
- 수작업을 줄이고 팀 내 의사소통 효율성을 향상시키는 것을 목표로 합니다.
"""

import os
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from notion_client import Client as NotionClient
from slack_sdk import WebClient
import dotenv

from service.slack import get_email_to_user_id

dotenv.load_dotenv()


NOTION_DATABASE_ID: str = "a9de18b3877c453a8e163c2ee1ff4137"
SLACK_CHANNEL_ID: str = "C02VA2LLXH9"


def get_pr_links(
    notion: NotionClient, pr_relations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """PR 관계 속성에서 PR 링크들과 병합 상태를 추출합니다."""
    pr_links_info: list[dict[str, Any]] = []
    for relation in pr_relations:
        pr_page_id: str = relation["id"]
        pr_page: dict[str, Any] = notion.pages.retrieve(page_id=pr_page_id)
        properties: dict[str, Any] = pr_page["properties"]

        url_property: dict[str, Any] = properties.get("_external_object_url", {})
        if "url" in url_property and url_property["url"]:
            pr_url: str = url_property["url"]
            # 'Merged At' 필드에서 병합 여부 추출
            merged_at_property: dict[str, Any] = properties.get("Merged At", {})
            is_merged: bool = False
            if merged_at_property.get("date") and merged_at_property["date"].get(
                "start"
            ):
                is_merged = True
            pr_links_info.append({"url": pr_url, "merged": is_merged})
        else:
            # URL 속성이 없는 경우 처리 로직을 추가할 수 있습니다.
            pass
    return pr_links_info


def format_pr_link(pr_info: dict[str, Any]) -> tuple[str, str | None]:
    """PR 링크를 포맷하고 레포지토리 이름을 추출하며 병합 상태에 따라 이모지를 추가합니다."""
    pr_url: str = pr_info["url"]
    is_merged: bool = pr_info["merged"]
    parsed_url = urlparse(pr_url)
    path_parts = parsed_url.path.strip("/").split("/")
    if len(path_parts) >= 4 and path_parts[2] == "pull":
        repo_name: str = path_parts[1]
        pr_number: str = path_parts[3]
        display_text: str = f"{repo_name}#{pr_number}"

        # 병합 상태에 따른 이모지 결정
        emoji: str = "✅" if is_merged else "❌"

        slack_link: str = f"<{pr_url}|{display_text}>{emoji}"
        return slack_link, repo_name
    else:
        # 예상되는 형식이 아닐 경우 원래 URL 반환
        return pr_url, None


def summarize_deployment(
    caller_slack_user_id: str | None = None,
):
    """
    1) Notion DB에서 오늘 배포 예정인 과업 목록 조회
    2) 담당자를 이메일로 매핑해서 Slack 멘션
    3) GitHub PR 링크 파싱
    4) 한 번에 정리된 메시지를 Slack에 전송
    """
    notion = NotionClient(auth=os.environ["NOTION_TOKEN"])
    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    email_to_user_id = get_email_to_user_id(slack_client)

    today_str = datetime.now().date().isoformat()  # "YYYY-MM-DD"

    # 1) 오늘 배포할 과업 목록 조회
    #    - 구성요소 필터링
    #      '기획' OR '디자인'이 포함되지 않은 과업
    #    - 상태 필터링
    #      '중단' 이 아닌 과업
    shared_filters = [
        {
            "property": "구성요소",
            "multi_select": {"does_not_contain": tag},
        }
        for tag in ["기획", "디자인"]
    ] + [
        {
            "property": "상태",
            "status": {"does_not_equal": "중단"},
        }
    ]

    #    - 날짜 필터링
    #      '배포 예정 날짜' ?? '종료일' == 오늘
    #      <=> {('배포 예정 날짜' == 오늘) OR ('배포 예정 날짜' == NULL AND '종료일' == 오늘)}
    date_conditions = [
        # '배포 예정 날짜' == 오늘
        [
            {
                "property": "배포 예정 날짜",
                "date": {"equals": today_str},
            }
        ],
        # '배포 예정 날짜' == NULL AND '종료일' == 오늘
        [
            {"property": "배포 예정 날짜", "date": {"is_empty": True}},
            {
                "property": "종료일",
                "date": {"equals": today_str},
            },
        ],
    ]

    # Notion API에서 Compound filter confitions 를 최대 2 Levels deep으로 지원합니다.
    # 이에 따라, 구성요소 필터링을 분배법칙으로 OR 연산의 operand 각각에 적용합니다.
    # 참조: https://developers.notion.com/reference/post-database-query-filter#compound-filter-conditions
    or_filters = [{"and": cond + shared_filters} for cond in date_conditions]

    query_result = notion.databases.query(
        **{
            "database_id": NOTION_DATABASE_ID,
            "filter": {
                "or": or_filters,
            },
        }
    )

    tasks = query_result.get("results", [])
    if not tasks:
        # 오늘 배포할 과업이 없으면 Slack 메시지 전송 후 종료
        slack_client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text="오늘 예정된 배포가 없네요. 놓치신 과업은 없으실까요?\n(/summarize-deployment 명령어를 사용해보세요!)",
        )
        print("No tasks scheduled for deployment today.")
        return

    # 여러 PR에서 뽑은 레포지토리들
    repos_to_deploy: set[str] = set()

    # 메시지 헤더 & 호출자 멘션
    if caller_slack_user_id:
        message = f"오늘 배포 예정 과업! (by <@{caller_slack_user_id}>)\n"
    else:
        message = "오늘 배포 예정 과업!\n"

    for task in tasks:
        props = task["properties"]

        # 2) 담당자(people 속성)에서 이메일을 추출하여 Slack 멘션 처리
        assignees = props.get("담당자", {}).get("people", [])
        if assignees:
            notion_email = assignees[0].get("person", {}).get("email")
            if notion_email:
                slack_user_id = email_to_user_id.get(notion_email)
                if slack_user_id:
                    assignee_mention = f"<@{slack_user_id}>"
                else:
                    assignee_mention = notion_email
            else:
                assignee_mention = "Unknown Email"
        else:
            assignee_mention = "Unassigned"

        # 3) 과업 제목, 노션 페이지 링크
        title_prop = props.get("제목", {})
        if "title" in title_prop and title_prop["title"]:
            task_title = title_prop["title"][0]["plain_text"]
        else:
            task_title = "No Title"

        # 노션 페이지 URL
        notion_page_url = task["url"]
        task_title_link = f"<{notion_page_url}|{task_title}>"

        # 4) GitHub PR 링크 정보(가정: "GitHub 풀 리퀘스트"라는 URL 속성이 있다고 가정)
        pr_link_property: dict[str, Any] = props.get("GitHub 풀 리퀘스트", {})
        pr_relations: list[dict[str, Any]] = pr_link_property.get("relation", [])
        pr_links_info: list[dict[str, Any]] = get_pr_links(notion, pr_relations)

        # PR 링크 포맷 및 레포지토리 이름 수집
        formatted_pr_links: list[str] = []
        for pr_info in pr_links_info:
            formatted_link, repo_name = format_pr_link(pr_info)
            formatted_pr_links.append(formatted_link)
            if repo_name:
                repos_to_deploy.add(repo_name)

        pr_links_str: str = (
            ", ".join(formatted_pr_links) if formatted_pr_links else "No PR Link"
        )

        # 메시지 구성
        message_line: str = f"{assignee_mention} {task_title_link} ({pr_links_str})\n"
        message += message_line

    # 레포지토리 안내 추가
    if repos_to_deploy:
        message += "\n아래의 레포지토리를 배포해주세요 :ship:\n"
        for repo in sorted(repos_to_deploy):
            message += f"• {repo}\n"

    message += "\n(/summarize-deployment 명령어를 사용해보세요!)\n"

    # 최종 메시지 전송
    slack_client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=message)
    print("Message sent to Slack.")


if __name__ == "__main__":
    summarize_deployment()
