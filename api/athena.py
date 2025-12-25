"""
AWS Athena API 래퍼 함수들
"""

import os
import time
import boto3
from botocore.exceptions import ClientError


def get_athena_client():
    """
    AWS Athena 클라이언트를 생성하여 반환합니다.

    Returns:
        boto3.client: Athena 클라이언트
    """
    return boto3.client(
        "athena",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("AWS_REGION", "ap-northeast-2"),
    )


def execute_query(query: str, database: str) -> str:
    """
    Athena 쿼리를 실행하고 실행 ID를 반환합니다.

    Args:
        query: 실행할 SQL 쿼리
        database: 쿼리를 실행할 데이터베이스 (필수)

    Returns:
        str: 쿼리 실행 ID
    """
    client = get_athena_client()
    output_location = os.environ.get("ATHENA_OUTPUT_LOCATION")

    response = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": database},
        ResultConfiguration={"OutputLocation": output_location},
    )

    return response["QueryExecutionId"]


def get_query_status(query_execution_id: str) -> dict:
    """
    쿼리 실행 상태를 조회합니다.

    Args:
        query_execution_id: 쿼리 실행 ID

    Returns:
        dict: 쿼리 실행 상태 정보
    """
    client = get_athena_client()
    response = client.get_query_execution(QueryExecutionId=query_execution_id)
    return response["QueryExecution"]


def wait_for_query_completion(
    query_execution_id: str, max_wait_seconds: int = 300
) -> dict:
    """
    쿼리가 완료될 때까지 대기합니다.

    Args:
        query_execution_id: 쿼리 실행 ID
        max_wait_seconds: 최대 대기 시간 (초)

    Returns:
        dict: 쿼리 실행 상태 정보

    Raises:
        TimeoutError: 대기 시간 초과
        RuntimeError: 쿼리 실행 실패
    """
    start_time = time.time()

    while True:
        status_info = get_query_status(query_execution_id)
        state = status_info["Status"]["State"]

        if state == "SUCCEEDED":
            return status_info
        elif state in ["FAILED", "CANCELLED"]:
            reason = status_info["Status"].get("StateChangeReason", "Unknown reason")
            raise RuntimeError(f"Query {state}: {reason}")

        if time.time() - start_time > max_wait_seconds:
            raise TimeoutError(f"Query execution timed out after {max_wait_seconds}s")

        time.sleep(1)


def get_query_results(query_execution_id: str, max_results: int = 1000) -> dict:
    """
    쿼리 결과를 가져옵니다.

    Args:
        query_execution_id: 쿼리 실행 ID
        max_results: 가져올 최대 결과 수

    Returns:
        dict: 쿼리 결과 (원본 Athena 응답)
    """
    client = get_athena_client()
    response = client.get_query_results(
        QueryExecutionId=query_execution_id, MaxResults=max_results
    )
    return response


def execute_and_wait(query: str, database: str, max_wait_seconds: int = 300) -> dict:
    """
    쿼리를 실행하고 완료될 때까지 대기한 후 결과를 반환합니다.

    Args:
        query: 실행할 SQL 쿼리
        database: 쿼리를 실행할 데이터베이스 (필수)
        max_wait_seconds: 최대 대기 시간 (초)

    Returns:
        dict: 쿼리 결과 (원본 Athena 응답)
    """
    query_execution_id = execute_query(query, database)
    wait_for_query_completion(query_execution_id, max_wait_seconds)
    return get_query_results(query_execution_id)
