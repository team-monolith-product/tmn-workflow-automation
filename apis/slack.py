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
    response = requests.get(url, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'})
    response.raise_for_status()
    data: Dict[str, Any] = response.json()
    if not data.get('ok'):
        raise Exception(f"Error fetching Slack users: {data.get('error')}")
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
