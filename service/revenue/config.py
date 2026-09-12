"""매출 배치의 원본 위치와 정규화 규칙."""

from pathlib import Path

import yaml

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge" / "revenue"


def load_sources() -> dict:
    """매출장 위치·탭별 열 배치를 읽는다."""
    return yaml.safe_load((KNOWLEDGE_DIR / "sources.yml").read_text())


def load_rules() -> dict:
    """상품 분류·수기 보정·사업 별칭을 읽는다."""
    return yaml.safe_load((KNOWLEDGE_DIR / "rules.yml").read_text())
