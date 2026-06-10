"""sync_dependabot_automerge 스크립트의 config 검증 및 ruleset 생성 로직 테스트"""

import pytest
import yaml

from scripts.github_admin.sync_dependabot_automerge import (
    CALLER_CONTENT,
    RULESET_NAME,
    build_ci_gate_ruleset,
    load_automerge_config,
)


class TestLoadAutomergeConfig:
    def test_loads_and_validates(self):
        config = load_automerge_config()
        assert len(config) > 0
        for repo_config in config.values():
            assert len(repo_config["required_checks"]) > 0

    def test_rejects_empty_required_checks(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text('{"some-repo": {"required_checks": []}}')
        monkeypatch.setattr(
            "scripts.github_admin.sync_dependabot_automerge.SCRIPT_DIR", tmp_path
        )
        monkeypatch.setattr(
            "scripts.github_admin.sync_dependabot_automerge.CONFIG_FILE", "config.json"
        )
        with pytest.raises(ValueError, match="required_checks"):
            load_automerge_config()


class TestBuildCiGateRuleset:
    def test_shape(self):
        ruleset = build_ci_gate_ruleset(["test", "lint"])
        assert ruleset["name"] == RULESET_NAME
        assert ruleset["enforcement"] == "active"
        assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]

        (rule,) = ruleset["rules"]
        assert rule["type"] == "required_status_checks"
        assert rule["parameters"]["required_status_checks"] == [
            {"context": "test"},
            {"context": "lint"},
        ]


class TestCallerContent:
    def test_is_valid_yaml_calling_central_workflow(self):
        workflow = yaml.safe_load(CALLER_CONTENT)
        job = workflow["jobs"]["automerge"]
        assert job["uses"] == (
            "team-monolith-product/tmn-gh-actions"
            "/.github/workflows/dependabot-automerge.yml@main"
        )
        assert workflow["permissions"] == {
            "contents": "write",
            "pull-requests": "write",
        }
