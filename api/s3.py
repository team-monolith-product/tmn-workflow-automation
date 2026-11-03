"""
AWS S3 API 래퍼 함수들
"""

import os

import boto3


def get_object(bucket: str, key: str) -> bytes:
    """
    S3 객체 본문을 받아옵니다.

    Args:
        bucket: 버킷 이름
        key: 객체 키

    Returns:
        bytes: 객체 본문
    """
    client = boto3.client(
        "s3", region_name=os.environ.get("AWS_REGION", "ap-northeast-2")
    )
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
    client = boto3.client(
        "s3", region_name=os.environ.get("AWS_REGION", "ap-northeast-2")
    )
    paginator = client.get_paginator("list_objects_v2")
    return [
        item
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for item in page.get("Contents", [])
    ]
