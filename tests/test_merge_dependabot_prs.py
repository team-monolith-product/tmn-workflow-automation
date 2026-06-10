"""merge_dependabot_prs 스크립트의 커밋 메타데이터 파싱 및 병합 정책 테스트"""

from scripts.github_admin.merge_dependabot_prs import (
    evaluate_ci,
    evaluate_updates,
    is_minor_or_lower_update,
    parse_dependabot_commit,
    parse_version,
)

# 실제 Dependabot 커밋 메시지 (jce-codle-jlext#212)
SINGLE_UPDATE_MESSAGE = """\
chore(deps): bump shell-quote from 1.8.1 to 1.8.4

Bumps [shell-quote](https://github.com/ljharb/shell-quote) from 1.8.1 to 1.8.4.
- [Changelog](https://github.com/ljharb/shell-quote/blob/main/CHANGELOG.md)
- [Commits](https://github.com/ljharb/shell-quote/compare/v1.8.1...v1.8.4)

---
updated-dependencies:
- dependency-name: shell-quote
  dependency-version: 1.8.4
  dependency-type: indirect
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

# 실제 Dependabot 그룹 커밋 메시지, 의존성 제거 포함 (ops-workflow-automation#5)
GROUP_WITH_REMOVAL_MESSAGE = """\
chore(deps): bump serialize-javascript and terser-webpack-plugin

Removes [serialize-javascript](https://github.com/yahoo/serialize-javascript). It's no longer used after updating ancestor dependency [terser-webpack-plugin](https://github.com/webpack/minimizer-webpack-plugin). These dependencies need to be updated together.


Removes `serialize-javascript`

Updates `terser-webpack-plugin` from 5.3.16 to 5.6.1
- [Release notes](https://github.com/webpack/minimizer-webpack-plugin/releases)

---
updated-dependencies:
- dependency-name: serialize-javascript
  dependency-version:
  dependency-type: indirect
- dependency-name: terser-webpack-plugin
  dependency-version: 5.6.1
  dependency-type: indirect
...

Signed-off-by: dependabot[bot] <support@github.com>
"""


class TestParseVersion:
    def test_semver(self):
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_v_prefix(self):
        assert parse_version("v4") == (4,)

    def test_no_version(self):
        assert parse_version("latest") is None


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


class TestParseDependabotCommit:
    def test_single_update(self):
        assert parse_dependabot_commit(SINGLE_UPDATE_MESSAGE) == [
            {"name": "shell-quote", "before": "1.8.1", "after": "1.8.4"}
        ]

    def test_group_with_removal(self):
        assert parse_dependabot_commit(GROUP_WITH_REMOVAL_MESSAGE) == [
            {"name": "serialize-javascript", "before": None, "after": None},
            {"name": "terser-webpack-plugin", "before": "5.3.16", "after": "5.6.1"},
        ]

    def test_title_only_message(self):
        # docker/pip 계열은 본문 없이 제목에만 버전 변경이 있음 (jce-js-dockerfile#105)
        message = (
            "chore(deps): bump jupyterlab from 4.1.8 to 4.5.7 in /jupyterlab4\n"
            "\n"
            "---\n"
            "updated-dependencies:\n"
            "- dependency-name: jupyterlab\n"
            "  dependency-version: 4.5.7\n"
            "  dependency-type: direct:production\n"
            "...\n"
            "\n"
            "Signed-off-by: dependabot[bot] <support@github.com>\n"
        )
        assert parse_dependabot_commit(message) == [
            {"name": "jupyterlab", "before": "4.1.8", "after": "4.5.7"}
        ]

    def test_requirement_range_update_has_no_version_pair(self):
        # "Update rails requirement from ~> 7.0 to ~> 7.1" 형태는 의존성명과 from
        # 사이에 단어가 끼어 매칭되지 않음 → 버전 쌍 없음으로 보수적 차단됨
        message = (
            "chore(deps): update rails requirement from ~> 7.0 to ~> 7.1\n"
            "\n"
            "---\n"
            "updated-dependencies:\n"
            "- dependency-name: rails\n"
            "  dependency-version: 7.1.0\n"
            "  dependency-type: direct:production\n"
            "...\n"
        )
        assert parse_dependabot_commit(message) == [
            {"name": "rails", "before": None, "after": None}
        ]

    def test_no_metadata_block(self):
        assert parse_dependabot_commit("일반 커밋 메시지") is None


class TestEvaluateUpdates:
    def test_patch_update_eligible(self):
        eligible, _ = evaluate_updates(
            [{"name": "shell-quote", "before": "1.8.1", "after": "1.8.4"}]
        )
        assert eligible is True

    def test_major_update_blocked(self):
        eligible, reason = evaluate_updates(
            [{"name": "puma", "before": "5.6.9", "after": "7.2.1"}]
        )
        assert eligible is False
        assert "puma" in reason

    def test_zero_major_minor_blocked(self):
        eligible, _ = evaluate_updates(
            [{"name": "authlib", "before": "0.15.5", "after": "0.16.0"}]
        )
        assert eligible is False

    def test_removal_blocked(self):
        eligible, reason = evaluate_updates(
            [
                {"name": "serialize-javascript", "before": None, "after": None},
                {"name": "terser-webpack-plugin", "before": "5.3.16", "after": "5.6.1"},
            ]
        )
        assert eligible is False
        assert "serialize-javascript" in reason

    def test_group_all_safe_eligible(self):
        eligible, _ = evaluate_updates(
            [
                {"name": "a", "before": "1.2.3", "after": "1.3.0"},
                {"name": "b", "before": "2.0.0", "after": "2.0.1"},
            ]
        )
        assert eligible is True

    def test_group_with_major_blocked(self):
        eligible, _ = evaluate_updates(
            [
                {"name": "a", "before": "1.2.3", "after": "1.3.0"},
                {"name": "b", "before": "2.0.0", "after": "3.0.0"},
            ]
        )
        assert eligible is False

    def test_none_blocked(self):
        eligible, _ = evaluate_updates(None)
        assert eligible is False

    def test_unparseable_version_blocked(self):
        eligible, _ = evaluate_updates(
            [{"name": "a", "before": "latest", "after": "1.0.0"}]
        )
        assert eligible is False


class TestEvaluateCi:
    def test_all_success(self):
        state = evaluate_ci(
            [("test", "completed", "success"), ("lint", "completed", "skipped")]
        )
        assert state == "success"

    def test_failure(self):
        state = evaluate_ci(
            [("test", "completed", "success"), ("lint", "completed", "failure")]
        )
        assert state == "failure"

    def test_pending(self):
        assert evaluate_ci([("test", "in_progress", None)]) == "pending"

    def test_no_checks_at_all(self):
        assert evaluate_ci([]) == "none"

    def test_only_skipped_is_not_ci_evidence(self):
        assert evaluate_ci([("test", "completed", "skipped")]) == "none"
