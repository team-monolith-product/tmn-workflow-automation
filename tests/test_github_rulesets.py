from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.github_admin import add_ruleset


@pytest.fixture
def ruleset_setup(monkeypatch):
    repos = [
        SimpleNamespace(name="active-repo", archived=False),
        SimpleNamespace(name="archived-repo", archived=True),
    ]
    config = {
        repo.name: {
            "skip": ["ruleset.json"],
            "add": ["ruleset_main_no_review.json"],
        }
        for repo in repos
    }
    monkeypatch.setattr(add_ruleset, "get_github_client", lambda: object())
    monkeypatch.setattr(add_ruleset, "get_org_name", lambda: "test-org")
    monkeypatch.setattr(add_ruleset, "get_organization", lambda *args: object())
    monkeypatch.setattr(
        add_ruleset,
        "get_all_repos",
        lambda org, include_archived=False: [
            repo for repo in repos if include_archived or not repo.archived
        ],
    )
    monkeypatch.setattr(add_ruleset, "load_repo_rulesets_config", lambda: config)
    apply_defaults = Mock(return_value=(1, 0))
    apply_specific = Mock(return_value=(1, 0))
    monkeypatch.setattr(add_ruleset, "apply_ruleset_to_repos", apply_defaults)
    monkeypatch.setattr(add_ruleset, "apply_repo_specific_rulesets", apply_specific)
    return config, apply_defaults, apply_specific


@pytest.mark.parametrize("repo_name", [None, "active-repo"])
def test_archived_repo_config_is_valid_but_never_applied(
    monkeypatch, ruleset_setup, repo_name
):
    config, apply_defaults, apply_specific = ruleset_setup
    args = ["add_ruleset.py", "--dry-run"]
    if repo_name is not None:
        args.extend(["--repo", repo_name])
    monkeypatch.setattr("sys.argv", args)

    add_ruleset.main()

    assert len(apply_defaults.call_args_list) == 2
    for call in apply_defaults.call_args_list:
        assert call.args[1] == ["active-repo"]
        assert "archived-repo" not in (call.kwargs["skip_repos"] or set())
    apply_specific.assert_called_once_with(
        "test-org", {"active-repo": config["active-repo"]}, True
    )


def test_unknown_config_repo_is_rejected_before_applying(monkeypatch, ruleset_setup):
    config, apply_defaults, apply_specific = ruleset_setup
    config["missing-repo"] = {}
    monkeypatch.setattr("sys.argv", ["add_ruleset.py", "--repo", "active-repo"])

    with pytest.raises(ValueError, match="missing-repo"):
        add_ruleset.main()

    apply_defaults.assert_not_called()
    apply_specific.assert_not_called()


def test_archived_repo_cannot_be_selected_for_application(monkeypatch, ruleset_setup):
    _, apply_defaults, apply_specific = ruleset_setup
    monkeypatch.setattr("sys.argv", ["add_ruleset.py", "--repo", "archived-repo"])

    with pytest.raises(SystemExit) as exc:
        add_ruleset.main()

    assert exc.value.code == 1
    apply_defaults.assert_not_called()
    apply_specific.assert_not_called()
