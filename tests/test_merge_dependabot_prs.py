"""merge_dependabot_prs 스크립트의 버전 파싱 및 병합 판단 로직 테스트"""

from unittest.mock import MagicMock

from scripts.github_admin.merge_dependabot_prs import (
    get_ci_state,
    is_minor_or_lower_update,
    parse_update_versions,
    parse_version,
)


class TestParseVersion:
    def test_semver(self):
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_v_prefix(self):
        assert parse_version("v4") == (4,)

    def test_ruby_requirement(self):
        assert parse_version("~> 7.0") == (7, 0)

    def test_no_version(self):
        assert parse_version("latest") is None


class TestParseUpdateVersions:
    def test_bump_title(self):
        title = "Bump lodash from 4.17.20 to 4.17.21"
        assert parse_update_versions(title) == ((4, 17, 20), (4, 17, 21))

    def test_requirement_title(self):
        title = "Update rails requirement from ~> 7.0 to ~> 7.1"
        assert parse_update_versions(title) == ((7, 0), (7, 1))

    def test_github_actions_title(self):
        title = "Bump actions/checkout from 3 to 4"
        assert parse_update_versions(title) == ((3,), (4,))

    def test_group_update_title(self):
        title = "Bump the npm_and_yarn group with 3 updates"
        assert parse_update_versions(title) is None

    def test_directory_prefix_title(self):
        title = "Bump axios from 1.6.0 to 1.7.2 in /frontend"
        assert parse_update_versions(title) == ((1, 6, 0), (1, 7, 2))


class TestIsMinorOrLowerUpdate:
    def test_minor_update(self):
        assert is_minor_or_lower_update((1, 2, 0), (1, 3, 0)) is True

    def test_patch_update(self):
        assert is_minor_or_lower_update((1, 2, 0), (1, 2, 1)) is True

    def test_major_update(self):
        assert is_minor_or_lower_update((1, 9, 0), (2, 0, 0)) is False

    def test_zero_major_minor_update_is_breaking(self):
        assert is_minor_or_lower_update((0, 1, 0), (0, 2, 0)) is False

    def test_zero_major_patch_update(self):
        assert is_minor_or_lower_update((0, 1, 0), (0, 1, 5)) is True


def make_commit(check_runs: list[tuple[str, str | None]], combined_state: str | None):
    """check run (status, conclusion) 목록과 combined status로 mock 커밋 생성"""
    commit = MagicMock()
    runs = []
    for status, conclusion in check_runs:
        run = MagicMock()
        run.status = status
        run.conclusion = conclusion
        runs.append(run)
    commit.get_check_runs.return_value = runs

    combined = MagicMock()
    combined.total_count = 1 if combined_state else 0
    combined.state = combined_state
    commit.get_combined_status.return_value = combined
    return commit


class TestGetCiState:
    def test_all_success(self):
        commit = make_commit([("completed", "success"), ("completed", "skipped")], None)
        assert get_ci_state(commit) == "success"

    def test_failure(self):
        commit = make_commit([("completed", "success"), ("completed", "failure")], None)
        assert get_ci_state(commit) == "failure"

    def test_pending(self):
        commit = make_commit([("in_progress", None)], None)
        assert get_ci_state(commit) == "pending"

    def test_no_ci(self):
        commit = make_commit([], None)
        assert get_ci_state(commit) == "none"

    def test_combined_status_failure(self):
        commit = make_commit([("completed", "success")], "failure")
        assert get_ci_state(commit) == "failure"

    def test_combined_status_only_success(self):
        commit = make_commit([], "success")
        assert get_ci_state(commit) == "success"
