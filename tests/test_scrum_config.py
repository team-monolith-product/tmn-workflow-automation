import os
import tempfile
from pathlib import Path

import pytest
import yaml

from service.scrum_config import (
    NotionDBConfig,
    PersonalScrum,
    ScrumConfig,
    ScrumSquad,
    load_scrum_config,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """각 테스트 전후로 lru_cache 초기화"""
    load_scrum_config.cache_clear()
    yield
    load_scrum_config.cache_clear()


def test_load_actual_config():
    """실제 scrum_config.yaml을 로드하여 기본 구조 검증"""
    config_path = str(Path(__file__).parent.parent / "scrum_config.yaml")
    config = load_scrum_config(config_path)

    assert isinstance(config, ScrumConfig)
    assert config.channel_id == "C09277NGUET"
    assert len(config.notion_databases) == 2
    assert "main" in config.notion_databases
    assert "explore" in config.notion_databases
    assert len(config.squads) == 5
    assert len(config.personal_scrums) == 1


def test_squad_order_preserved():
    """squads 순서가 YAML 정의 순서대로 유지되는지 검증"""
    config_path = str(Path(__file__).parent.parent / "scrum_config.yaml")
    config = load_scrum_config(config_path)

    handles = [s.handle for s in config.squads]
    assert handles == ["기획", "fe", "be", "ie", "탐색"]


def test_notion_db_reference():
    """스쿼드가 올바른 NotionDBConfig를 참조하는지 검증"""
    config_path = str(Path(__file__).parent.parent / "scrum_config.yaml")
    config = load_scrum_config(config_path)

    fe_squad = next(s for s in config.squads if s.handle == "fe")
    explore_squad = next(s for s in config.squads if s.handle == "탐색")

    assert fe_squad.notion_db.name == "main"
    assert explore_squad.notion_db.name == "explore"
    assert explore_squad.notion_db.properties.pr is None


def test_pr_warning_config():
    """PR 경고 설정이 올바르게 로드되는지 검증"""
    config_path = str(Path(__file__).parent.parent / "scrum_config.yaml")
    config = load_scrum_config(config_path)

    squad_map = {s.handle: s for s in config.squads}
    assert squad_map["기획"].pr_warning is False
    assert squad_map["fe"].pr_warning is True
    assert squad_map["탐색"].pr_warning is False


def test_invalid_db_reference():
    """존재하지 않는 notion_db 참조 시 ValueError"""
    raw = {
        "channel_id": "C000",
        "notion_databases": {},
        "squads": [
            {
                "handle": "test",
                "display_name": "Test",
                "slack_usergroup_id": "S000",
                "notion_db": "nonexistent",
            }
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        f.flush()
        with pytest.raises(ValueError, match="nonexistent"):
            load_scrum_config(f.name)
    os.unlink(f.name)


def test_env_var_override():
    """SCRUM_CONFIG_PATH 환경변수로 경로 오버라이드"""
    config_path = str(Path(__file__).parent.parent / "scrum_config.yaml")
    os.environ["SCRUM_CONFIG_PATH"] = config_path
    try:
        config = load_scrum_config()
        assert isinstance(config, ScrumConfig)
    finally:
        del os.environ["SCRUM_CONFIG_PATH"]


def test_personal_scrum():
    """개인 스크럼 설정 검증"""
    config_path = str(Path(__file__).parent.parent / "scrum_config.yaml")
    config = load_scrum_config(config_path)

    assert config.personal_scrums[0].name == "이창환"
    assert config.personal_scrums[0].slack_user_id == "U02HT4EU4VD"
