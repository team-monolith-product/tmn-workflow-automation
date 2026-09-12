from pathlib import Path

import yaml

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge" / "revenue"


def load_sources() -> dict:
    return yaml.safe_load((KNOWLEDGE_DIR / "sources.yml").read_text())


def load_rules() -> dict:
    return yaml.safe_load((KNOWLEDGE_DIR / "rules.yml").read_text())
