"""
스크럼 설정 로드

scrum_config.yaml에서 스크럼 관련 설정을 로드합니다.
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml


@dataclass(frozen=True)
class NotionDBProperties:
    """Notion DB 프로퍼티 이름 매핑"""

    title: str
    status: str
    assignee: str
    timeline: str
    pr: str | None = None


@dataclass(frozen=True)
class NotionDBConfig:
    """Notion DB 설정"""

    name: str
    data_source_id: str
    properties: NotionDBProperties
    in_progress_statuses: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScrumSquad:
    """스크럼 참여 스쿼드"""

    handle: str
    display_name: str
    slack_usergroup_id: str
    slack_channel_id: str
    notion_db: NotionDBConfig
    pr_warning: bool = True


@dataclass(frozen=True)
class PersonalScrum:
    """개인 스크럼"""

    name: str
    slack_user_id: str
    slack_channel_id: str


@dataclass(frozen=True)
class ScrumConfig:
    """스크럼 전체 설정"""

    notion_databases: dict[str, NotionDBConfig]
    squads: list[ScrumSquad]
    personal_scrums: list[PersonalScrum]


def _parse_config(raw: dict) -> ScrumConfig:
    """YAML dict를 ScrumConfig로 변환"""
    # Notion databases
    notion_databases = {}
    for name, db_raw in raw.get("notion_databases", {}).items():
        props = db_raw["properties"]
        notion_databases[name] = NotionDBConfig(
            name=name,
            data_source_id=db_raw["data_source_id"],
            properties=NotionDBProperties(
                title=props["title"],
                status=props["status"],
                assignee=props["assignee"],
                timeline=props["timeline"],
                pr=props.get("pr"),
            ),
            in_progress_statuses=db_raw.get("in_progress_statuses", []),
        )

    # Squads
    squads = []
    for squad_raw in raw.get("squads", []):
        db_name = squad_raw["notion_db"]
        if db_name not in notion_databases:
            raise ValueError(
                f"스쿼드 '{squad_raw['handle']}'가 참조하는 "
                f"notion_db '{db_name}'가 notion_databases에 없습니다."
            )
        squads.append(
            ScrumSquad(
                handle=squad_raw["handle"],
                display_name=squad_raw["display_name"],
                slack_usergroup_id=squad_raw["slack_usergroup_id"],
                slack_channel_id=squad_raw["slack_channel_id"],
                notion_db=notion_databases[db_name],
                pr_warning=squad_raw.get("pr_warning", True),
            )
        )

    # Personal scrums
    personal_scrums = [
        PersonalScrum(
            name=p["name"],
            slack_user_id=p["slack_user_id"],
            slack_channel_id=p["slack_channel_id"],
        )
        for p in raw.get("personal_scrums", [])
    ]

    return ScrumConfig(
        notion_databases=notion_databases,
        squads=squads,
        personal_scrums=personal_scrums,
    )


@lru_cache(maxsize=1)
def load_scrum_config(config_path: str | None = None) -> ScrumConfig:
    """
    스크럼 설정 파일 로드

    Args:
        config_path: YAML 파일 경로. None이면 환경변수 또는 기본 경로 사용.

    Returns:
        ScrumConfig: 파싱된 설정
    """
    if config_path is None:
        config_path = os.environ.get(
            "SCRUM_CONFIG_PATH",
            str(Path(__file__).parent.parent / "scrum_config.yaml"),
        )
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    return _parse_config(raw)
