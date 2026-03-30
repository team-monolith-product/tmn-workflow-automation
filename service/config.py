"""
통합 설정 로드

config.yaml에서 조직, 스크럼, 작업 알림 설정을 로드합니다.
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml


# --- Notion DB ---


@dataclass(frozen=True)
class NotionDBProperties:
    """Notion DB 프로퍼티 이름 매핑"""

    title: str
    status: str
    assignee: str
    timeline: str
    start_date: str | None = None
    end_date: str | None = None
    pr: str | None = None


@dataclass(frozen=True)
class NotionDBConfig:
    """Notion DB 설정"""

    name: str
    data_source_id: str
    properties: NotionDBProperties
    pending_statuses: list[str] = field(default_factory=list)
    in_progress_statuses: list[str] = field(default_factory=list)


# --- 조직 SOT ---


@dataclass(frozen=True)
class Squad:
    """조직 단위 스쿼드"""

    handle: str
    slack_usergroup_id: str
    notion_db: NotionDBConfig


# --- 스크럼 ---


@dataclass(frozen=True)
class ScrumSquadConfig:
    """스크럼 참여 스쿼드 설정"""

    squad: Squad
    display_name: str
    channel_id: str
    pr_warning: bool = True


@dataclass(frozen=True)
class PersonalScrum:
    """개인 스크럼"""

    name: str
    slack_user_id: str
    channel_id: str


@dataclass(frozen=True)
class ScrumConfig:
    """스크럼 설정"""

    squads: list[ScrumSquadConfig]
    personal_scrums: list[PersonalScrum]


# --- 작업 알림 ---


@dataclass(frozen=True)
class TaskAlertPipeline:
    """작업 알림 파이프라인"""

    name: str
    channel_id: str
    squads: list[Squad]
    alerts: list[str]


@dataclass(frozen=True)
class TaskAlertsConfig:
    """작업 알림 설정"""

    pipelines: list[TaskAlertPipeline]


# --- 전체 ---


@dataclass(frozen=True)
class AppConfig:
    """전체 앱 설정"""

    notion_databases: dict[str, NotionDBConfig]
    squads: list[Squad]
    scrum: ScrumConfig
    task_alerts: TaskAlertsConfig


def _parse_config(raw: dict) -> AppConfig:
    """YAML dict를 AppConfig로 변환"""
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
                start_date=props.get("start_date"),
                end_date=props.get("end_date"),
                pr=props.get("pr"),
            ),
            pending_statuses=db_raw.get("pending_statuses", []),
            in_progress_statuses=db_raw.get("in_progress_statuses", []),
        )

    # Squads (조직 SOT)
    squads = []
    squad_by_handle: dict[str, Squad] = {}
    for squad_raw in raw.get("squads", []):
        db_name = squad_raw["notion_db"]
        if db_name not in notion_databases:
            raise ValueError(
                f"스쿼드 '{squad_raw['handle']}'가 참조하는 "
                f"notion_db '{db_name}'가 notion_databases에 없습니다."
            )
        squad = Squad(
            handle=squad_raw["handle"],
            slack_usergroup_id=squad_raw["slack_usergroup_id"],
            notion_db=notion_databases[db_name],
        )
        squads.append(squad)
        squad_by_handle[squad.handle] = squad

    # Scrum
    scrum_raw = raw.get("scrum", {})
    scrum_squads = []
    for ss_raw in scrum_raw.get("squads", []):
        handle = ss_raw["handle"]
        if handle not in squad_by_handle:
            raise ValueError(f"scrum.squads의 handle '{handle}'이 squads에 없습니다.")
        scrum_squads.append(
            ScrumSquadConfig(
                squad=squad_by_handle[handle],
                display_name=ss_raw["display_name"],
                channel_id=ss_raw["channel_id"],
                pr_warning=ss_raw.get("pr_warning", True),
            )
        )
    personal_scrums = [
        PersonalScrum(
            name=p["name"],
            slack_user_id=p["slack_user_id"],
            channel_id=p["channel_id"],
        )
        for p in scrum_raw.get("personal_scrums", [])
    ]
    scrum = ScrumConfig(squads=scrum_squads, personal_scrums=personal_scrums)

    # Task alerts
    ta_raw = raw.get("task_alerts", {})
    pipelines = []
    for pl_raw in ta_raw.get("pipelines", []):
        pl_squads = []
        for handle in pl_raw.get("squads", []):
            if handle not in squad_by_handle:
                raise ValueError(
                    f"task_alerts pipeline '{pl_raw['name']}'의 "
                    f"squad handle '{handle}'이 squads에 없습니다."
                )
            pl_squads.append(squad_by_handle[handle])
        pipelines.append(
            TaskAlertPipeline(
                name=pl_raw["name"],
                channel_id=pl_raw["channel_id"],
                squads=pl_squads,
                alerts=pl_raw.get("alerts", []),
            )
        )
    task_alerts = TaskAlertsConfig(pipelines=pipelines)

    return AppConfig(
        notion_databases=notion_databases,
        squads=squads,
        scrum=scrum,
        task_alerts=task_alerts,
    )


@lru_cache(maxsize=1)
def load_config(config_path: str | None = None) -> AppConfig:
    """
    설정 파일 로드

    Args:
        config_path: YAML 파일 경로. None이면 환경변수 또는 기본 경로 사용.

    Returns:
        AppConfig: 파싱된 설정
    """
    if config_path is None:
        config_path = os.environ.get(
            "CONFIG_PATH",
            str(Path(__file__).parent.parent / "config.yaml"),
        )
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    return _parse_config(raw)
