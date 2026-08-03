"""
지식베이스 database와 롤을 만들고 비밀번호를 Secrets Manager에 등록합니다.

운영 인스턴스(enk-rds-a-prd)에 DDL을 칩니다. 반드시 --dry-run 으로 먼저
확인하세요. 여러 번 실행해도 같은 결과가 되도록 만들었습니다.

앱 비밀번호는 이 스크립트가 생성해서 바로 Secrets Manager에 넣습니다.
사람 눈과 셸 히스토리를 거치지 않게 하려는 것이므로 어떤 경로로도 출력하지
않습니다.

마스터 비밀번호는 argv에 넣지 마세요. 같은 머신의 다른 프로세스가 ps로 봅니다.

사용법:
    PGPASSWORD="$(aws secretsmanager get-secret-value \\
      --secret-id 'rds!db-e519b75f-b7d9-4b24-bc30-eeca66446df8' \\
      --region ap-northeast-2 --query SecretString --output text \\
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')" \\
      python scripts/bootstrap_knowledge_db.py --dry-run
"""

import argparse
import json
import secrets

import boto3
import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row

MASTER_HOST = "enk-rds-a-prd.cqvc5d9pa2x8.ap-northeast-2.rds.amazonaws.com"
MASTER_DSN = f"postgresql://postgres@{MASTER_HOST}:5432/postgres?sslmode=require"

DATABASE = "knowledge"
ROLE = "knowledge_write_prd"

AWS_REGION = "ap-northeast-2"
REMOTE_SECRET = "tmn-secret-prd"
SECRET_PROPERTY = "workflow_knowledge_database_password"


def _connect(dbname: str) -> psycopg.Connection:
    """마스터 계정으로 지정한 database에 접속합니다.

    CREATE DATABASE는 트랜잭션 안에서 실행할 수 없어 autocommit으로 엽니다.

    Args:
        dbname: 접속할 database 이름

    Returns:
        psycopg.Connection: autocommit 커넥션
    """
    dsn = MASTER_DSN.replace("/postgres?", f"/{dbname}?")
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def _exists(conn: psycopg.Connection, sql: str, param: str) -> bool:
    """단일 존재 여부 질의를 수행합니다.

    Args:
        conn: 커넥션
        sql: SELECT 1 형태의 질의
        param: 바인딩 값

    Returns:
        bool: 존재하면 True
    """
    with conn.cursor() as cur:
        cur.execute(sql, (param,))
        return cur.fetchone() is not None


def create_database(dry_run: bool) -> None:
    """knowledge database를 만듭니다.

    Args:
        dry_run: True면 상태만 출력합니다.
    """
    with _connect("postgres") as conn:
        if _exists(conn, "SELECT 1 FROM pg_database WHERE datname = %s", DATABASE):
            print(f"database {DATABASE}: 이미 있음")
            return
        if dry_run:
            print(f"[dry-run] CREATE DATABASE {DATABASE}")
            return
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{DATABASE}"')
        print(f"database {DATABASE}: 생성함")


def upsert_role(password: str, dry_run: bool) -> None:
    """앱 롤을 만들고 비밀번호를 설정합니다.

    이미 있으면 비밀번호만 바꿉니다. 중간에 실패해 아무도 모르는 비밀번호가
    남더라도 재실행으로 복구되게 하려는 것입니다.

    Args:
        password: 설정할 비밀번호
        dry_run: True면 상태만 출력합니다.
    """
    with _connect("postgres") as conn:
        exists = _exists(conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", ROLE)
        action = "비밀번호 갱신" if exists else "생성"
        if dry_run:
            print(f"[dry-run] 롤 {ROLE}: {action}")
            return

        # CREATE/ALTER ROLE은 유틸리티 구문이라 바인딩 파라미터를 받지 않는다.
        # sql.Literal로 안전하게 인용해 문장에 직접 넣는다.
        verb = "ALTER" if exists else "CREATE"
        statement = sql.SQL("{verb} ROLE {role} {opts} PASSWORD {password}").format(
            verb=sql.SQL(verb),
            role=sql.Identifier(ROLE),
            opts=sql.SQL("WITH LOGIN" if exists else "LOGIN"),
            password=sql.Literal(password),
        )
        with conn.cursor() as cur:
            cur.execute(statement)
        print(f"롤 {ROLE}: {action}함")


def grant_and_extend(dry_run: bool) -> None:
    """knowledge database에 확장을 설치하고 권한을 부여합니다.

    Args:
        dry_run: True면 상태만 출력합니다.
    """
    if dry_run:
        print(f"[dry-run] {DATABASE}에 pg_bigm 설치, {ROLE}에 스키마 권한 부여")
        return

    with _connect(DATABASE) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_bigm")
            cur.execute(f'GRANT CONNECT ON DATABASE "{DATABASE}" TO "{ROLE}"')
            cur.execute(f'GRANT USAGE, CREATE ON SCHEMA public TO "{ROLE}"')
            # 마이그레이션이 만들 테이블에도 자동으로 권한이 붙게 한다.
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{ROLE}"'
            )
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f'GRANT USAGE, SELECT ON SEQUENCES TO "{ROLE}"'
            )
    print(f"{DATABASE}: pg_bigm 설치, {ROLE} 권한 부여함")


def register_password(password: str, dry_run: bool) -> None:
    """비밀번호를 tmn-secret-prd에 키 하나로 추가합니다.

    Secrets Manager에는 JSON 부분 수정 API가 없어 통째로 덮어써야 합니다.
    이 시크릿에는 전사 자격증명이 들어 있으므로 기존 키가 하나도 사라지지
    않았는지 확인한 뒤에만 씁니다.

    Args:
        password: 등록할 비밀번호
        dry_run: True면 변경 없이 검증만 합니다.
    """
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    current = json.loads(
        client.get_secret_value(SecretId=REMOTE_SECRET)["SecretString"]
    )

    already = SECRET_PROPERTY in current
    updated = dict(current)
    updated[SECRET_PROPERTY] = password

    # 우리가 건드리는 키 하나 말고는 값까지 그대로인지 확인한다.
    untouched = {k: v for k, v in current.items() if k != SECRET_PROPERTY}
    if any(updated.get(k) != v for k, v in untouched.items()):
        raise RuntimeError("기존 키의 값이 바뀝니다. 중단합니다.")
    if len(updated) != len(current) + (0 if already else 1):
        raise RuntimeError("키 개수가 예상과 다릅니다. 중단합니다.")

    # 원격 시크릿 이름과 키 이름은 파일 상단 상수로 보인다. 로그에 남길 이유가
    # 없고, 남기면 CodeQL이 민감 데이터 로깅으로 잡는다.
    print(
        f"원격 시크릿: 키 {len(current)}개 → {len(updated)}개, {'갱신' if already else '추가'}"
    )
    if dry_run:
        print("[dry-run] put-secret-value 생략")
        return

    client.put_secret_value(
        SecretId=REMOTE_SECRET, SecretString=json.dumps(updated, ensure_ascii=False)
    )
    print("원격 시크릿: 반영함")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="지식베이스 DB 부트스트랩")
    parser.add_argument(
        "--dry-run", action="store_true", help="변경하지 않고 수행할 작업만 출력"
    )
    args = parser.parse_args()

    password = secrets.token_urlsafe(32)

    create_database(args.dry_run)
    upsert_role(password, args.dry_run)
    grant_and_extend(args.dry_run)
    register_password(password, args.dry_run)


if __name__ == "__main__":
    main()
