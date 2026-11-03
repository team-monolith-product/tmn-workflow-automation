"""
AWS SES API 래퍼 함수들

SES v2 API 를 씁니다. v1 SendRawEmail 은 한 통에 10MB 까지라 첨부가 든
메일을 보내기에 작고, v2 는 40MB 까지 받습니다. IAM 권한은 ses:SendEmail 입니다.
"""

import os

import boto3


def send_mime(raw: bytes) -> dict:
    """
    MIME 원문 그대로 메일을 보냅니다. 받는 사람은 원문의 To 헤더에서 읽습니다.

    Args:
        raw: MIME 원문

    Returns:
        dict: SES 응답 (MessageId 포함)
    """
    client = boto3.client(
        "sesv2", region_name=os.environ.get("AWS_REGION", "ap-northeast-2")
    )
    return client.send_email(Content={"Raw": {"Data": raw}})
