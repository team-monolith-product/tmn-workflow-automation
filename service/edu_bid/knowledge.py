"""
지식 레이어 로더

knowledge/edu_bid/ 의 변동 자산(역량·자격·정책·소스)을 읽어온다.
파이프라인 단계는 이 객체를 읽기만 하고, 회사가 변하면 YAML 만 갱신한다.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge" / "edu_bid"


@dataclass(frozen=True)
class Knowledge:
    capability_profile: dict
    eligibility_ledger: dict
    scoring_policy: dict
    source_registry: dict

    @property
    def assets(self) -> list[dict]:
        return self.capability_profile.get("assets", [])

    @property
    def weights(self) -> dict:
        return self.scoring_policy.get("weights", {})

    @property
    def thresholds(self) -> dict:
        return self.scoring_policy.get("thresholds", {})

    @property
    def enabled_sources(self) -> list[dict]:
        return [s for s in self.source_registry.get("sources", []) if s.get("enabled")]


def _load(name: str, base: Path) -> dict:
    return yaml.safe_load((base / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def load_knowledge(knowledge_dir: str | None = None) -> Knowledge:
    base = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
    return Knowledge(
        capability_profile=_load("capability_profile.yaml", base),
        eligibility_ledger=_load("eligibility_ledger.yaml", base),
        scoring_policy=_load("scoring_policy.yaml", base),
        source_registry=_load("source_registry.yaml", base),
    )
