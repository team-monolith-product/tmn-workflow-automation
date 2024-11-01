# apis/slack.py

import os
import requests
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# Slack API 인증 정보
SLACK_BOT_TOKEN: Optional[str] = os.getenv('SLACK_BOT_TOKEN')

# Slack 헤더 설정
HEADERS: Dict[str, str] = {
    'Authorization': f'Bearer {SLACK_BOT_TOKEN}',
    'Content-Type': 'application/json'
}

def get_slack_users() -> List[Dict[str, Any]]:
    """Slack 사용자 목록을 가져옵니다."""
    url: str = 'https://slack.com/api/users.list'
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    data: Dict[str, Any] = response.json()
    if not data.get('ok'):
        raise Exception(f"Error fetching Slack users: {data.get('error')}")
    return data.get('members', [])

def get_slack_user_ids_in_channel(channel_id: str) -> List[str]:
    """Slack 채널의 사용자 ID 목록을 가져옵니다."""
    url: str = 'https://slack.com/api/conversations.members'
    payload: Dict[str, str] = {
        'channel': channel_id
    }
    response = requests.get(url, params=payload, headers=HEADERS)
    response.raise_for_status()
    data: Dict[str, Any] = response.json()
    if not data.get('ok'):
        raise Exception(f"Error fetching Slack channel members: {data.get('error')}")
    return data.get('members', [])

def send_slack_message(channel_id: str, message: str) -> Dict[str, Any]:
    """Slack 채널에 메시지를 전송합니다."""
    url: str = 'https://slack.com/api/chat.postMessage'
    payload: Dict[str, Any] = {
        'channel': channel_id,
        'text': message
    }
    response = requests.post(url, json=payload, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def get_user_info(user_id) -> Dict[str, Any]:
    """Slack 사용자 정보를 가져옵니다."""
    url: str = 'https://slack.com/api/users.info'
    payload: Dict[str, str] = {
        'user': user_id
    }
    response = requests.get(url, params=payload, headers=HEADERS)
    response.raise_for_status()
    data: Dict[str, Any] = response.json()
    if not data.get('ok'):
        raise Exception(f"Error fetching Slack user info: {data.get('error')}")
    return data.get('user', {})

def create_canvas():
    url = "https://slack.com/api/canvases.create"
    data = {
        "title": "신나는 데일리 스크럼!",
        "document_content": {"type": "markdown", "markdown": "> standalone canvas!"}
    }
    response = requests.post(url, headers=HEADERS, json=data)
    response_data = response.json()
    if response_data.get("ok"):
        return response_data["canvas_id"]
    else:
        raise Exception("캔버스 생성 실패: " + response_data.get("error", "Unknown error"))

def edit_canvas(canvas_id, changes):
    url = "https://slack.com/api/canvases.edit"
    data = {
        "canvas_id": canvas_id,
        "changes": changes
    }
    response = requests.post(url, headers=HEADERS, json=data)
    response_data = response.json()
    if not response_data.get("ok"):
        print(response_data)
        raise Exception("캔버스 편집 실패: " + response_data.get("error", "Unknown error"))

def lookup_sections(canvas_id):
    url = "https://slack.com/api/canvases.sections.lookup"
    data = {
        "canvas_id": canvas_id,
        "criteria": {
            "contains_text": " "
        }
    }
    response = requests.post(url, headers=HEADERS, json=data)
    response_data = response.json()

    if response_data.get("ok"):
        return response_data["sections"]
    else :
        print(response_data)
        raise Exception("캔버스 섹션 조회 실패: " + response_data.get("error", "Unknown error"))
