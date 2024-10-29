

"""
프로젝트 설명:
이 파이썬 스크립트는 오늘 배포 예정인 과업들을 노션(Notion) 데이터베이스에서 가져와, 지정된 슬랙(Slack) 채널에 포맷된 메시지를 전송합니다.
메시지에는 담당자가 멘션되고, 관련된 GitHub 풀 리퀘스트 링크가 포함됩니다.

ChatGPT의 도움으로 개발되었습니다. (https://chatgpt.com/share/671b5c5f-1710-8002-9843-c0bfe87377c5)

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

- 적절한 인증과 API 토큰의 안전한 처리를 보장합니다.
- 이름 대신 이메일을 기반으로 슬랙 사용자들을 매칭하여 정확도를 향상시킵니다.

특이사항:
- 스크립트는 노션의 'people' 속성에 접근하여 담당자 이메일을 추출합니다.
- 슬랙 API 스코프에 `users:read.email`을 포함하여 사용자 이메일 정보에 접근합니다.
- API 키와 토큰과 같은 모든 민감한 정보는 스크립트에 하드코딩하지 않고 환경 변수나 `.env` 파일을 통해 관리합니다.
- 사용자 이메일이 없거나 슬랙 사용자와 매칭되지 않는 경우를 처리하기 위한 에러 핸들링이 포함되어 있습니다.
- API 호출 제한을 주의하고, 슬랙 앱에 필요한 권한이 부여되었는지 확인합니다.
- 이 스크립트는 Flask 앱이 아닌 독립 실행형 파이썬 스크립트로 실행되도록 설계되었습니다.

사용 방법:
- 모든 의존성을 설치하세요 (`requests`, `python-dotenv`).
- 필요한 환경 변수를 설정하세요: `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`.
- `python your_script_name.py` 명령으로 스크립트를 실행하세요.

참고:
- 팀원들에게 오늘의 배포 과업에 대해 알리기 위한 워크플로우를 자동화하기 위해 개발되었습니다.
- 수작업을 줄이고 팀 내 의사소통 효율성을 향상시키는 것을 목표로 합니다.
"""
import os
import requests
import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional, Set, Tuple

# 환경 변수 로드
load_dotenv()

# Notion API 인증 정보
NOTION_API_KEY: Optional[str] = os.getenv('NOTION_API_KEY')
NOTION_DATABASE_ID: str = 'a9de18b3877c453a8e163c2ee1ff4137'

# Slack API 인증 정보
SLACK_BOT_TOKEN: Optional[str] = os.getenv('SLACK_BOT_TOKEN')
SLACK_CHANNEL_ID: str = 'C02VA2LLXH9'

def get_today_tasks() -> List[Dict[str, Any]]:
    """오늘 배포 예정인 노션 과업들을 가져옵니다."""
    url: str = f'https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query'
    headers: Dict[str, str] = {
        'Authorization': f'Bearer {NOTION_API_KEY}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }
    today: str = datetime.datetime.now().date().isoformat()
    payload: Dict[str, Any] = {
        "filter": {
            "property": "배포 예정 날짜",
            "date": {
                "equals": today
            }
        }
    }
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    data: Dict[str, Any] = response.json()
    return data['results']

def get_slack_users() -> List[Dict[str, Any]]:
    """Slack 사용자 목록을 가져옵니다."""
    url: str = 'https://slack.com/api/users.list'
    headers: Dict[str, str] = {
        'Authorization': f'Bearer {SLACK_BOT_TOKEN}'
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data: Dict[str, Any] = response.json()
    if not data.get('ok'):
        raise Exception(f"Error fetching Slack users: {data.get('error')}")
    return data.get('members', [])

def get_slack_user_id(notion_user_email: str, slack_users: List[Dict[str, Any]]) -> Optional[str]:
    """노션 사용자 이메일을 기반으로 Slack 사용자 ID를 찾습니다."""
    for user in slack_users:
        profile: Dict[str, Any] = user.get('profile', {})
        slack_user_email: Optional[str] = profile.get('email')
        if slack_user_email and slack_user_email.lower() == notion_user_email.lower():
            return user['id']
    return None

def send_slack_message(message: str) -> Dict[str, Any]:
    """Slack 채널에 메시지를 전송합니다."""
    url: str = 'https://slack.com/api/chat.postMessage'
    headers: Dict[str, str] = {
        'Authorization': f'Bearer {SLACK_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }
    payload: Dict[str, Any] = {
        'channel': SLACK_CHANNEL_ID,
        'text': message
    }
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()

def get_page(page_id: str) -> Dict[str, Any]:
    """노션 페이지의 상세 정보를 가져옵니다."""
    url: str = f'https://api.notion.com/v1/pages/{page_id}'
    headers: Dict[str, str] = {
        'Authorization': f'Bearer {NOTION_API_KEY}',
        'Notion-Version': '2022-06-28',
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def get_pr_links(pr_relations: List[Dict[str, Any]]) -> List[str]:
    """PR 관계 속성에서 PR 링크들을 추출합니다."""
    pr_links: List[str] = []
    for relation in pr_relations:
        pr_page_id: str = relation['id']
        pr_page: Dict[str, Any] = get_page(pr_page_id)
        properties: Dict[str, Any] = pr_page['properties']
        url_property: Dict[str, Any] = properties.get('_external_object_url', {})
        if 'url' in url_property and url_property['url']:
            pr_links.append(url_property['url'])
        else:
            # URL 속성이 없는 경우 처리 로직을 추가할 수 있습니다.
            pass
    return pr_links

def format_pr_link(pr_url: str) -> Tuple[str, Optional[str]]:
    """PR 링크를 포맷하고 레포지토리 이름을 추출합니다."""
    parsed_url = urlparse(pr_url)
    path_parts = parsed_url.path.strip('/').split('/')
    if len(path_parts) >= 4 and path_parts[2] == 'pull':
        repo_name: str = path_parts[1]
        pr_number: str = path_parts[3]
        display_text: str = f"{repo_name}#{pr_number}"
        slack_link: str = f"<{pr_url}|{display_text}>"
        return slack_link, repo_name
    else:
        # 예상되는 형식이 아닐 경우 원래 URL 반환
        return pr_url, None

def main() -> None:
    tasks: List[Dict[str, Any]] = get_today_tasks()
    if not tasks:
        print("No tasks scheduled for deployment today.")
        send_slack_message("오늘 예정된 배포가 없네요. 놓치신 과업은 없으실까요?")
        return

    # Slack 사용자 목록을 한 번만 가져옵니다.
    slack_users: List[Dict[str, Any]] = get_slack_users()

    # 배포해야 할 레포지토리 이름을 저장할 집합
    repos_to_deploy: Set[str] = set()

    message: str = "오늘 배포 예정 과업!\n"
    for task in tasks:
        properties: Dict[str, Any] = task['properties']

        # 담당자 정보 가져오기
        assignees: List[Dict[str, Any]] = properties.get('담당자', {}).get('people', [])
        if assignees:
            assignee: Dict[str, Any] = assignees[0]
            notion_user_email: Optional[str] = assignee.get('person', {}).get('email')
            if notion_user_email:
                slack_user_id: Optional[str] = get_slack_user_id(notion_user_email, slack_users)
                if slack_user_id:
                    assignee_mention: str = f"<@{slack_user_id}>"
                else:
                    assignee_mention = notion_user_email
            else:
                assignee_mention = "Unknown Email"
        else:
            assignee_mention = "Unassigned"

        # 과업 제목 가져오기
        title_property: Dict[str, Any] = properties.get('제목', {})
        if 'title' in title_property and title_property['title']:
            task_title: str = title_property['title'][0]['plain_text']
        else:
            task_title = "No Title"

        # 노션 페이지 URL 생성
        task_id: str = task['id']
        notion_page_url: str = f"https://www.notion.so/{task_id.replace('-', '')}"
        task_title_link: str = f"<{notion_page_url}|{task_title}>"

        # PR 링크 가져오기
        pr_link_property: Dict[str, Any] = properties.get('GitHub 풀 리퀘스트', {})
        pr_relations: List[Dict[str, Any]] = pr_link_property.get('relation', [])
        pr_links: List[str] = get_pr_links(pr_relations)

        # PR 링크 포맷 및 레포지토리 이름 수집
        formatted_pr_links: List[str] = []
        for pr_link in pr_links:
            formatted_link, repo_name = format_pr_link(pr_link)
            formatted_pr_links.append(formatted_link)
            if repo_name:
                repos_to_deploy.add(repo_name)

        pr_links_str: str = ', '.join(formatted_pr_links) if formatted_pr_links else "No PR Link"

        # 메시지 구성
        message_line: str = f"{assignee_mention} {task_title_link} ({pr_links_str})\n"
        message += message_line

    if repos_to_deploy:
        message += "\n아래의 레포지토리를 배포해주세요 :ship:\n"
        for repo in sorted(repos_to_deploy):
            message += f"• {repo}\n"

    # 슬랙에 메시지 전송
    send_slack_message(message)
    print("Message sent to Slack.")

if __name__ == "__main__":
    main()