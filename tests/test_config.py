import os
import tempfile
from pathlib import Path

import pytest
import yaml

from service.config import (
    AppConfig,
    NotionDBConfig,
    Squad,
    load_config,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """각 테스트 전후로 lru_cache 초기화"""
    load_config.cache_clear()
    yield
    load_config.cache_clear()


CONFIG_PATH = str(Path(__file__).parent.parent / "config.yaml")


def test_load_config():
    """config.yaml 로드 및 기본 구조 검증"""
    config = load_config(CONFIG_PATH)

    assert isinstance(config, AppConfig)
    assert len(config.notion_databases) == 5
    assert len(config.squads) == 5
    assert len(config.scrum.squads) == 4
    assert len(config.scrum.personal_scrums) == 1
    assert len(config.task_alerts.pipelines) == 2


def test_squad_references_notion_db():
    """스쿼드가 올바른 NotionDBConfig를 참조하는지 검증"""
    config = load_config(CONFIG_PATH)
    squad_map = {s.handle: s for s in config.squads}

    assert squad_map["코들"].notion_db.name == "main"
    assert squad_map["해커톤"].notion_db.name == "hackathon"
    assert squad_map["ie"].notion_db.name == "infra"
    assert squad_map["탐색"].notion_db.name == "explore"
    assert squad_map["콘텐츠"].notion_db.name == "contents"


def test_notion_db_properties():
    """DB별 프로퍼티 매핑 검증"""
    config = load_config(CONFIG_PATH)

    main = config.notion_databases["main"]
    assert main.properties.title == "제목"
    assert main.properties.start_date == "시작일"
    assert main.properties.end_date == "종료일"
    assert main.properties.pr == "GitHub 풀 리퀘스트"
    assert main.pending_statuses == ["대기"]
    assert main.in_progress_statuses == ["진행", "리뷰"]

    hackathon = config.notion_databases["hackathon"]
    assert hackathon.properties.title == "이름"
    assert hackathon.properties.timeline == "타임라인"
    assert hackathon.properties.start_date == "시작일"
    assert hackathon.properties.end_date == "종료일"
    assert hackathon.properties.pr == "GitHub 풀 리퀘스트"
    assert hackathon.pending_statuses == ["대기"]
    assert hackathon.in_progress_statuses == ["진행", "리뷰"]


def test_scrum_config():
    """스크럼 설정 검증"""
    config = load_config(CONFIG_PATH)

    handles = [s.squad.handle for s in config.scrum.squads]
    assert handles == ["코들", "해커톤", "탐색", "ie"]

    codle = config.scrum.squads[0]
    assert codle.squad.display_name == ":codle_bird: 코들 스쿼드"
    assert codle.channel_id == "C09277NGUET"
    assert codle.pr_warning is True
    assert codle.squad.notion_db.name == "main"

    assert config.scrum.personal_scrums[0].name == "CTO"


def test_task_alert_pipelines():
    """작업 알림 파이프라인 검증"""
    config = load_config(CONFIG_PATH)

    product = config.task_alerts.pipelines[0]
    assert product.name == "제품 본부"
    assert product.channel_id == "C087PDC9VG8"
    assert [ps.squad.handle for ps in product.pipeline_squads] == ["코들", "해커톤", "ie"]
    ie = product.pipeline_squads[2]
    assert "alert_overdue_tasks" in ie.alerts
    assert "alert_no_upcoming_tasks" in ie.alerts
    codle = product.pipeline_squads[0]
    assert "alert_no_upcoming_tasks" not in codle.alerts

    contents = config.task_alerts.pipelines[1]
    assert contents.name == "콘텐츠 본부"
    assert [ps.squad.handle for ps in contents.pipeline_squads] == ["콘텐츠"]
    assert "alert_schedule_feasibility" not in contents.pipeline_squads[0].alerts


def test_invalid_squad_db_reference():
    """존재하지 않는 notion_db 참조 시 ValueError"""
    raw = {
        "notion_databases": {},
        "squads": [
            {
                "handle": "test",
                "slack_usergroup_id": "S000",
                "notion_db": "nonexistent",
            }
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        f.flush()
        with pytest.raises(ValueError, match="nonexistent"):
            load_config(f.name)
    os.unlink(f.name)


def test_invalid_scrum_handle_reference():
    """scrum에서 존재하지 않는 squad handle 참조 시 ValueError"""
    raw = {
        "notion_databases": {
            "db1": {
                "data_source_id": "x",
                "properties": {
                    "title": "t",
                    "status": "s",
                    "assignee": "a",
                    "timeline": "tl",
                },
            }
        },
        "squads": [{"handle": "a", "slack_usergroup_id": "S1", "notion_db": "db1"}],
        "scrum": {
            "squads": [
                {"handle": "nonexistent", "channel_id": "C0"}
            ]
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        f.flush()
        with pytest.raises(ValueError, match="nonexistent"):
            load_config(f.name)
    os.unlink(f.name)


def test_env_var_override():
    """CONFIG_PATH 환경변수로 경로 오버라이드"""
    os.environ["CONFIG_PATH"] = CONFIG_PATH
    try:
        config = load_config()
        assert isinstance(config, AppConfig)
    finally:
        del os.environ["CONFIG_PATH"]
