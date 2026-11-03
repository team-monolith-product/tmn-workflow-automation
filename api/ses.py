"""
AWS SES API 래퍼 함수들

IAM 정책이 ses:SendRawEmail 만 허용하므로 SES v1 API 를 씁니다.
SES v2 의 send_email 은 ses:SendEmail 권한을 요구합니다.
"""

import os

import boto3


def send_raw_email(raw: bytes) -> dict:
    """
    MIME 원문 그대로 메일을 보냅니다. 받는 사람은 원문의 To 헤더에서 읽습니다.

    Args:
        raw: MIME 원문

    Returns:
        dict: SES 응답 (MessageId 포함)
    """
    client = boto3.client(
        "ses", region_name=os.environ.get("AWS_REGION", "ap-northeast-2")
    )
    return client.send_raw_email(RawMessage={"Data": raw})
