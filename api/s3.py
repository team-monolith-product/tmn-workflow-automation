"""
AWS S3 API 래퍼 함수들
"""

import os

import boto3


def _client():
    # 작업 스레드에서 부른다. boto3 기본 세션은 여러 스레드가 동시에 클라이언트를
    # 만들면 깨질 수 있어 부를 때마다 세션을 따로 만든다.
    return boto3.session.Session().client(
        "s3", region_name=os.environ.get("AWS_REGION", "ap-northeast-2")
    )


def get_object(bucket: str, key: str) -> bytes:
    """
    S3 객체 본문을 받아옵니다.

    Args:
        bucket: 버킷 이름
        key: 객체 키

    Returns:
        bytes: 객체 본문
    """
    client = _client()
    response = client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def list_objects(bucket: str, prefix: str) -> list[dict]:
    """
    prefix 아래 객체 목록을 끝까지 받아옵니다.

    Args:
        bucket: 버킷 이름
        prefix: 객체 키 prefix

    Returns:
        list[dict]: ListObjectsV2 의 Contents 를 이어붙인 것 (Key, LastModified 등)
    """
    client = _client()
    paginator = client.get_paginator("list_objects_v2")
    return [
        item
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for item in page.get("Contents", [])
    ]
