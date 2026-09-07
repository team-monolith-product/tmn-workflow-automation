"""
지식베이스 스키마 마이그레이션을 적용합니다.

migrations/knowledge/*.sql 을 파일명 순서대로 적용하고, 적용 이력을
schema_migrations 테이블에 남깁니다. 이미 적용된 파일은 건너뜁니다.

CREATE EXTENSION 은 rds_superuser 권한이 필요하므로 마스터 계정 DSN 으로
실행해야 합니다. 개발 DB 없이 운영 인스턴스에 바로 적용하므로 반드시
--dry-run 으로 먼저 확인합니다.

사용법:
    python scripts/migrate_knowledge_db.py --dry-run
    python scripts/migrate_knowledge_db.py --dsn "postgresql://..."
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import pathlib

from dotenv import load_dotenv

from service.db import connect, fetch_all

MIGRATION_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "migrations" / "knowledge"
)

CREATE_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


def list_migration_files() -> list[pathlib.Path]:
    """적용 대상 마이그레이션 파일을 파일명 순으로 반환합니다.

    Returns:
        list[pathlib.Path]: .sql 파일 목록
    """
    return sorted(MIGRATION_DIR.glob("*.sql"))


def migrate(dsn: str | None, dry_run: bool) -> None:
    """미적용 마이그레이션을 순서대로 적용합니다.

    Args:
        dsn: 접속 문자열. None이면 KNOWLEDGE_DATABASE_URL을 씁니다.
        dry_run: True면 적용하지 않고 대상만 출력합니다.
    """
    files = list_migration_files()
    if not files:
        print(f"마이그레이션 파일이 없습니다: {MIGRATION_DIR}")
        return

    with connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_HISTORY_TABLE)

        applied = {
            row["filename"]
            for row in fetch_all(conn, "SELECT filename FROM schema_migrations")
        }

        pending = [f for f in files if f.name not in applied]
        if not pending:
            print(f"적용할 마이그레이션이 없습니다. (이미 적용됨 {len(applied)}개)")
            return

        for path in pending:
            if dry_run:
                print(f"[dry-run] {path.name} ({path.stat().st_size} bytes)")
                continue

            print(f"적용 중: {path.name}")
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
            print(f"적용 완료: {path.name}")

        if dry_run:
            conn.rollback()
            print(
                f"[dry-run] {len(pending)}개가 적용 대상입니다. 변경사항은 롤백했습니다."
            )


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="지식베이스 스키마 마이그레이션")
    parser.add_argument(
        "--dsn",
        help="접속 문자열. 생략하면 KNOWLEDGE_DATABASE_URL 환경 변수를 사용합니다.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="적용하지 않고 대상만 출력"
    )
    args = parser.parse_args()

    migrate(args.dsn, args.dry_run)


if __name__ == "__main__":
    main()
