"""
지식베이스 Postgres 연결을 제공하는 Service Layer입니다.
"""

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row


def get_dsn() -> str:
    """지식베이스 DSN을 반환합니다.

    Returns:
        str: postgresql:// 로 시작하는 접속 문자열
    """
    return os.environ["KNOWLEDGE_DATABASE_URL"]


@contextmanager
def connect(
    dsn: str | None = None, read_only: bool | None = None
) -> Iterator[psycopg.Connection]:
    """지식베이스에 접속합니다.

    블록을 정상 종료하면 커밋하고, 예외가 나면 롤백합니다.

    Args:
        dsn: 접속 문자열. 생략하면 KNOWLEDGE_DATABASE_URL을 씁니다.
        read_only: True면 트랜잭션을 READ ONLY로 엽니다. 적재와 질의가 같은
            롤로 접속하므로 권한으로는 갈리지 않습니다. 생략하면 psycopg가
            BEGIN에 아무 모드도 붙이지 않아 서버 기본값을 따릅니다.

    Yields:
        psycopg.Connection: dict_row 팩토리가 설정된 커넥션
    """
    with psycopg.connect(dsn or get_dsn(), row_factory=dict_row) as conn:
        conn.read_only = read_only
        yield conn


def fetch_all(
    conn: psycopg.Connection, sql: str, params: tuple | dict | None = None
) -> list[dict[str, Any]]:
    """조회 결과를 전부 반환합니다.

    Args:
        conn: 커넥션
        sql: 실행할 SQL
        params: 바인딩 파라미터

    Returns:
        list[dict[str, Any]]: 행 목록
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(
    conn: psycopg.Connection, sql: str, params: tuple | dict | None = None
) -> dict[str, Any] | None:
    """조회 결과의 첫 행을 반환합니다.

    Args:
        conn: 커넥션
        sql: 실행할 SQL
        params: 바인딩 파라미터

    Returns:
        dict[str, Any] | None: 첫 행. 결과가 없으면 None
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
