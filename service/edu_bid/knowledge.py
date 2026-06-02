"""
지식 레이어 로더

전략 지식(자산·정량규격·전략)의 원본은 DB(SoT)다 — 어드민에서 편집하면 새 버전이 쌓이고
여기서 활성 버전을 읽는다. DB 에 문서가 없거나 DATABASE_URL 미설정이면 YAML 로 폴백한다
(부트스트랩·로컬 개발). 사업유형·소스(work_types/source_registry)는 아직 YAML 만 쓴다.

DB 우선 3문서: capability_profile(공유), eligibility_ledger(공유), scoring_policy(트랙별).
"""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge" / "edu_bid"


@dataclass(frozen=True)
class Knowledge:
    track_key: str  # 이 지식이 어느 트랙용인지 (자산·정량실적 트랙뷰 기준)
    capability_profile: dict
    eligibility_ledger: dict
    scoring_policy: dict  # 트랙별 (tracks/<key>.yaml)
    source_registry: dict
    work_types: dict

    @property
    def assets(self) -> list[dict]:
        """이 트랙에 태그된 내부 자산만. capability_profile 은 공유(SSOT)이고,
        각 자산의 tracks: 태그로 트랙뷰를 만든다. 평가 프롬프트에 주입된다."""
        return [
            a
            for a in self.capability_profile.get("assets", [])
            if self.track_key in a.get("tracks", [])
        ]

    @property
    def track_performance(self) -> list[dict]:
        """이 트랙의 정량 실적(실적금액 증명 가능 계약). 평가 프롬프트의 실적상태 근거.
        자격(업종·직생·인증)은 법인 단위라 공유하고, 정량 실적만 트랙별로 둔다."""
        return self.eligibility_ledger.get("track_performance", {}).get(
            self.track_key, []
        )

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


def _yaml_file(section: str, track: str) -> str:
    """DB 우선 섹션의 YAML 폴백 경로."""
    if section == "scoring_policy":
        return f"tracks/{track}.yaml"
    return f"{section}.yaml"


def _document(section: str, track: str, base: Path) -> dict:
    """DB(SoT) 활성 버전 → 없으면 YAML 폴백. DATABASE_URL 미설정이면 바로 YAML.

    DB 가 원본이므로 캐시하지 않는다(어드민 편집이 다음 실행에 바로 반영되도록).
    """
    if os.environ.get("DATABASE_URL"):
        from .knowledge_store import get_active_document

        doc = get_active_document(section, track)
        if doc is not None:
            return doc
    return _load(_yaml_file(section, track), base)


def load_knowledge(track_key: str, knowledge_dir: str | None = None) -> Knowledge:
    """트랙 지식 조립. 자산·정량규격·전략은 DB(SoT, YAML 폴백), 사업유형·소스는 YAML."""
    base = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
    return Knowledge(
        track_key=track_key,
        capability_profile=_document("capability_profile", "", base),
        eligibility_ledger=_document("eligibility_ledger", "", base),
        scoring_policy=_document("scoring_policy", track_key, base),
        source_registry=_load("source_registry.yaml", base),
        work_types=_load("work_types.yaml", base),
    )
