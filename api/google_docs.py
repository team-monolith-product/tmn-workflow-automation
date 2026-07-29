"""
Google Docs API 래퍼 함수
"""

import json
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/documents"]


def get_docs_service():
    """환경 변수의 서비스 계정 JSON으로 Docs v1 서비스를 생성한다."""
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def replace_all_text(
    document_id: str,
    find: str,
    replace: str,
    match_case: bool = True,
) -> dict:
    """
    문서에서 일치하는 모든 텍스트를 치환한다.

    Docs API의 다른 편집 요청은 문자 인덱스를 다루지만 replaceAllText는 그렇지 않아,
    편집 위치를 잘못 계산해 엉뚱한 곳을 고칠 위험이 없다.

    Args:
        document_id: 문서 ID
        find: 찾을 문자열
        replace: 바꿀 문자열
        match_case: 대소문자 구분 여부

    Returns:
        Docs API documents.batchUpdate 원본 응답
    """
    service = get_docs_service()
    return (
        service.documents()
        .batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": find, "matchCase": match_case},
                            "replaceText": replace,
                        }
                    }
                ]
            },
        )
        .execute()
    )
