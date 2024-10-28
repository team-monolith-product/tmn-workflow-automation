"""
이 스크립트는 개발 과정에서 **Notion 데이터베이스 ID가 정확한지 확인하고**, **검색이 정확하게 이루어지고 있는지 디버깅**하기 위해 작성되었습니다.

주요 필요성:
- 노션 API를 사용하여 접근 가능한 모든 데이터베이스를 검색함으로써, 현재 사용 중인 데이터베이스 ID가 올바른지 검증할 수 있습니다.
- 각 데이터베이스의 제목과 ID를 출력하여, 데이터베이스가 정상적으로 검색되고 있는지 확인할 수 있습니다.
- 이를 통해 API 호출이 제대로 작동하는지, 인증 정보나 요청 형식에 문제가 없는지 디버깅할 수 있습니다.

이 스크립트를 사용하면 개발자는 노션 API와의 통신이 원활한지 확인하고, 이후의 개발 작업에 필요한 정확한 데이터베이스 ID를 확보할 수 있습니다.
"""

import os
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Notion API credentials
NOTION_API_KEY = os.getenv('NOTION_API_KEY')

def search_databases():
    url = 'https://api.notion.com/v1/search'
    headers = {
        'Authorization': f'Bearer {NOTION_API_KEY}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }
    payload = {
        "filter": {
            "value": "database",
            "property": "object"
        }
    }
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    data = response.json()
    print(data)
    return data['results']

def main():
    databases = search_databases()
    for db in databases:
        db_id = db['id']
        db_title = db['title'][0]['plain_text'] if db['title'] else 'Untitled'
        print(f"Database Title: {db_title}")
        print(f"Database ID: {db_id}")
        print('-' * 40)

if __name__ == "__main__":
    main()
