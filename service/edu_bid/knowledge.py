"""
지식 레이어 로더

knowledge/edu_bid/ 의 변동 자산(역량·자격·소스·사업유형)은 트랙이 공유하고,
전략·점수정책은 tracks/<key>.yaml 로 트랙마다 따로 둔다. 파이프라인 단계는 이
객체를 읽기만 하고, 회사·전략이 변하면 코드가 아니라 YAML 만 갱신한다.
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
    scoring_policy: dict  # 트랙별 (tracks/<key>.yaml)
    source_registry: dict
    work_types: dict

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
def _load_shared(base: Path) -> dict:
    """트랙이 공유하는 지식 4종 (역량·자격·소스·사업유형). base 단위로 캐시."""
    return {
        "capability_profile": _load("capability_profile.yaml", base),
        "eligibility_ledger": _load("eligibility_ledger.yaml", base),
        "source_registry": _load("source_registry.yaml", base),
        "work_types": _load("work_types.yaml", base),
    }


@lru_cache(maxsize=8)
def load_knowledge(track_key: str, knowledge_dir: str | None = None) -> Knowledge:
    """트랙 지식 = 공유 4종 + tracks/<track_key>.yaml 의 전략·점수정책."""
    base = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
    return Knowledge(
        scoring_policy=_load(f"tracks/{track_key}.yaml", base),
        **_load_shared(base),
    )
