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
class SharedKnowledge:
    """트랙이 공유하는 지식 4종 (역량·자격·소스·사업유형). 트랙 무관.

    공유 상단부(prepare)가 수집·트리아지·사업유형·게이트에 쓴다. 전략·점수정책은 없다.
    """

    capability_profile: dict
    eligibility_ledger: dict
    source_registry: dict
    work_types: dict

    @property
    def enabled_sources(self) -> list[dict]:
        return [s for s in self.source_registry.get("sources", []) if s.get("enabled")]


@dataclass(frozen=True)
class Knowledge:
    """트랙 지식 = 공유 지식 + tracks/<key>.yaml 의 전략·점수정책. 트랙 하단부(run_track)용."""

    track_key: str  # 이 지식이 어느 트랙용인지 (자산·정량실적 트랙뷰 기준)
    shared: SharedKnowledge
    scoring_policy: dict  # 트랙별 (tracks/<key>.yaml)

    @property
    def assets(self) -> list[dict]:
        """이 트랙에 태그된 내부 자산만. capability_profile 은 공유(SSOT)이고,
        각 자산의 tracks: 태그로 트랙뷰를 만든다. 평가 프롬프트에 주입된다."""
        return [
            a
            for a in self.shared.capability_profile.get("assets", [])
            if self.track_key in a.get("tracks", [])
        ]

    @property
    def track_performance(self) -> list[dict]:
        """이 트랙의 정량 실적(실적금액 증명 가능 계약). 평가 프롬프트의 실적상태 근거.
        자격(업종·직생·인증)은 법인 단위라 공유하고, 정량 실적만 트랙별로 둔다."""
        return self.shared.eligibility_ledger.get("track_performance", {}).get(
            self.track_key, []
        )

    @property
    def weights(self) -> dict:
        return self.scoring_policy.get("weights", {})

    @property
    def thresholds(self) -> dict:
        return self.scoring_policy.get("thresholds", {})


def _load(name: str, base: Path) -> dict:
    return yaml.safe_load((base / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def load_shared_knowledge(knowledge_dir: str | None = None) -> SharedKnowledge:
    """트랙이 공유하는 지식 4종 (역량·자격·소스·사업유형). 공유 상단부(prepare)용."""
    base = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
    return SharedKnowledge(
        capability_profile=_load("capability_profile.yaml", base),
        eligibility_ledger=_load("eligibility_ledger.yaml", base),
        source_registry=_load("source_registry.yaml", base),
        work_types=_load("work_types.yaml", base),
    )


@lru_cache(maxsize=8)
def load_knowledge(track_key: str, knowledge_dir: str | None = None) -> Knowledge:
    """트랙 지식 = 공유 지식 + tracks/<track_key>.yaml 의 전략·점수정책."""
    base = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
    return Knowledge(
        track_key=track_key,
        shared=load_shared_knowledge(knowledge_dir),
        scoring_policy=_load(f"tracks/{track_key}.yaml", base),
    )
