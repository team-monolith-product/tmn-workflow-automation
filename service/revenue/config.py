"""
매출 facts 설정 파일 로드.

knowledge/revenue/ 아래 세 파일을 읽는다. 코드가 아니라 이 파일들이 기준이다.
  sources.yml     매출장 위치·열 배치·인식기준
  taxonomy.yml    분류 규칙과 연도 총계 검증값
  enrichment.yml  CRM 에서 끌어온 보강값. 매일 빌드는 조인만 한다
"""

from pathlib import Path
from typing import Any

import yaml

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge" / "revenue"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((KNOWLEDGE_DIR / name).read_text(encoding="utf-8")) or {}


def load_sources() -> dict[str, Any]:
    """매출장 위치·열 배치·인식기준."""
    return _load("sources.yml")


def load_taxonomy() -> dict[str, Any]:
    """분류 규칙. 뷰마다 새로 dict 를 만들어 돌려주므로 빌드가 덮어써도 파일은 안 바뀐다."""
    return _load("taxonomy.yml")


def load_enrichment(sources: dict[str, Any]) -> dict[str, Any]:
    """보강 테이블. 파일이 없으면 빈 dict — 보강 없이도 빌드는 돈다."""
    path = KNOWLEDGE_DIR.parent.parent / sources["enrichment"]["file"]
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
