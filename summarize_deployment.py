

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

# Load environment variables
load_dotenv()

# Notion API credentials
NOTION_API_KEY = os.getenv('NOTION_API_KEY')
NOTION_DATABASE_ID = os.getenv('NOTION_DATABASE_ID')

# Slack API credentials
SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN')
SLACK_CHANNEL_ID = os.getenv('SLACK_CHANNEL_ID')

def get_today_tasks():
    url = f'https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query'
    headers = {
        'Authorization': f'Bearer {NOTION_API_KEY}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }
    today = datetime.datetime.now().date().isoformat()
    payload = {
        "filter": {
            "property": "배포 예정 날짜",
            "date": {
                "equals": today
            }
        }
    }
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    data = response.json()
    return data['results']

def get_slack_user_id(notion_user_email):
    url = 'https://slack.com/api/users.list'
    headers = {
        'Authorization': f'Bearer {SLACK_BOT_TOKEN}'
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    users = response.json().get('members', [])
    for user in users:
        profile = user.get('profile', {})
        slack_user_email = profile.get('email')
        if slack_user_email and slack_user_email.lower() == notion_user_email.lower():
            return user['id']
    return None

def send_slack_message(message):
    url = 'https://slack.com/api/chat.postMessage'
    headers = {
        'Authorization': f'Bearer {SLACK_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }
    payload = {
        'channel': SLACK_CHANNEL_ID,
        'text': message
    }
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()

def get_page(page_id):
    url = f'https://api.notion.com/v1/pages/{page_id}'
    headers = {
        'Authorization': f'Bearer {NOTION_API_KEY}',
        'Notion-Version': '2022-06-28',
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def get_pr_links(pr_relations):
    pr_links = []
    for relation in pr_relations:
        pr_page_id = relation['id']
        pr_page = get_page(pr_page_id)
        properties = pr_page['properties']
        url_property = properties.get('_external_object_url', {})
        if 'url' in url_property and url_property['url']:
            pr_links.append(url_property['url'])
        else:
            # URL 속성이 없는 경우 다른 방식으로 처리하거나 로그를 남길 수 있습니다.
            pass
    return pr_links

def main():
    tasks = get_today_tasks()
    if not tasks:
        print("No tasks scheduled for deployment today.")
        send_slack_message("오늘 예정된 배포가 없네요. 놓치신 과업은 없으실까요?")
        return

    message = "오늘 배포 예정 과업!\n"
    for task in tasks:
        properties = task['properties']

        # Get Assignee
        assignees = properties.get('담당자', {}).get('people', [])
        if assignees:
            notion_user_email = assignees[0].get('person', {}).get('email')
            if notion_user_email:
                slack_user_id = get_slack_user_id(notion_user_email)
                if slack_user_id:
                    assignee_mention = f"<@{slack_user_id}>"
                else:
                    assignee_mention = notion_user_email
            else:
                assignee_mention = "Unknown Email"
        else:
            assignee_mention = "Unassigned"

        # Get Task Title
        title_property = properties.get('제목', {})
        if 'title' in title_property and title_property['title']:
            task_title = title_property['title'][0]['plain_text']
        else:
            task_title = "No Title"

        # Get PR Links
        pr_link_property = properties.get('GitHub 풀 리퀘스트', {})
        pr_relations = pr_link_property.get('relation', [])
        pr_links = get_pr_links(pr_relations)

        # PR 링크들을 문자열로 결합
        pr_links_str = ', '.join(pr_links) if pr_links else "No PR Link"

        # Construct the message line
        message_line = f"{assignee_mention} {task_title} ({pr_links_str})\n"
        message += message_line

    # Send the message to Slack
    send_slack_message(message)
    print("Message sent to Slack.")

if __name__ == "__main__":
    main()
