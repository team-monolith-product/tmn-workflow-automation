# apis/notion.py

import os
import requests
import datetime
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# Notion API 인증 정보
NOTION_API_KEY: Optional[str] = os.getenv('NOTION_API_KEY')

# Notion API 버전 및 헤더 설정
NOTION_VERSION: str = '2022-06-28'
HEADERS: Dict[str, str] = {
    'Authorization': f'Bearer {NOTION_API_KEY}',
    'Notion-Version': NOTION_VERSION,
    'Content-Type': 'application/json'
}

def get_today_tasks(database_id: str) -> List[Dict[str, Any]]:
    """오늘 배포 예정인 노션 과업들을 가져옵니다."""
    url: str = f'https://api.notion.com/v1/databases/{database_id}/query'
    today: str = datetime.datetime.now().date().isoformat()
    payload: Dict[str, Any] = {
        "filter": {
            "property": "배포 예정 날짜",
            "date": {
                "equals": today
            }
        }
    }
    response = requests.post(url, json=payload, headers=HEADERS)
    response.raise_for_status()
    data: Dict[str, Any] = response.json()
    return data['results']

def get_page(page_id: str) -> Dict[str, Any]:
    """노션 페이지의 상세 정보를 가져옵니다."""
    url: str = f'https://api.notion.com/v1/pages/{page_id}'
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def get_pr_links(pr_relations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PR 관계 속성에서 PR 링크들과 병합 상태를 추출합니다."""
    pr_links_info: List[Dict[str, Any]] = []
    for relation in pr_relations:
        pr_page_id: str = relation['id']
        pr_page: Dict[str, Any] = get_page(pr_page_id)
        properties: Dict[str, Any] = pr_page['properties']

        url_property: Dict[str, Any] = properties.get('_external_object_url', {})
        if 'url' in url_property and url_property['url']:
            pr_url: str = url_property['url']
            # 'Merged At' 필드에서 병합 여부 추출
            merged_at_property: Dict[str, Any] = properties.get('Merged At', {})
            is_merged: bool = False
            if merged_at_property.get('date') and merged_at_property['date'].get('start'):
                is_merged = True
            pr_links_info.append({
                'url': pr_url,
                'merged': is_merged
            })
        else:
            # URL 속성이 없는 경우 처리 로직을 추가할 수 있습니다.
            pass
    return pr_links_info