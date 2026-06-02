"""
현재 YAML 지식(자산·정량규격·전략)을 DB(SoT)로 시드한다.

DB 를 지식 원본으로 전환할 때 1회 실행. 멱등이라 여러 번 돌려도 안전하다
(직전 활성 버전과 내용이 같으면 새 버전을 만들지 않음).

    DATABASE_URL=... python scripts/seed_edu_bid_knowledge.py
    DATABASE_URL=... python scripts/seed_edu_bid_knowledge.py --dry-run

전제: 먼저 `alembic upgrade head` 로 테이블이 있어야 한다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse

from dotenv import load_dotenv

from service.config import load_config
from service.edu_bid import knowledge_store
from service.edu_bid.knowledge import _KNOWLEDGE_DIR, _load

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="edu_bid 지식 YAML → DB 시드")
    parser.add_argument("--dry-run", action="store_true", help="저장 없이 대상만 출력")
    args = parser.parse_args()

    cfg = load_config().education_bid_crawler
    track_keys = [t.key for t in cfg.tracks] if cfg else []

    # (section, track, yaml파일) — 공유 2종 + 트랙별 전략
    targets: list[tuple[str, str, str]] = [
        ("capability_profile", "", "capability_profile.yaml"),
        ("eligibility_ledger", "", "eligibility_ledger.yaml"),
    ]
    for key in track_keys:
        targets.append(("scoring_policy", key, f"tracks/{key}.yaml"))

    for section, track, yaml_file in targets:
        payload = _load(yaml_file, _KNOWLEDGE_DIR)
        label = f"{section}" + (f"[{track}]" if track else "")
        if args.dry_run:
            print(f"[seed] (dry-run) {label} ← {yaml_file}")
            continue
        version = knowledge_store.save_document(
            section, track, payload, author="seed", note=f"YAML 시드 ({yaml_file})"
        )
        print(f"[seed] {label} ← {yaml_file} → v{version}")


if __name__ == "__main__":
    main()
