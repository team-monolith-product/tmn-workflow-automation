"""develop 초기화 안내문 생성과 멘션 변환 테스트"""

from unittest.mock import MagicMock

from scripts.reset_develop import (
    LOCAL_CLEANUP_COMMAND,
    build_announcement,
    get_open_pr_authors,
    to_mentions,
)


def _pull(login: str) -> MagicMock:
    pull = MagicMock()
    pull.user.login = login
    return pull


def test_get_open_pr_authors_excludes_bots_and_duplicates():
    repo = MagicMock()
    repo.get_pulls.return_value = [
        _pull("shcshcshc"),
        _pull("renovate[bot]"),
        _pull("shcshcshc"),
        _pull("BrianPark314"),
    ]

    assert get_open_pr_authors(repo) == ["shcshcshc", "BrianPark314"]
    repo.get_pulls.assert_called_once_with(state="open")


def test_to_mentions_falls_back_to_login_when_unmapped():
    mentions = to_mentions(["shcshcshc", "unknown-login"])

    assert mentions == ["<@U03M4FZ6P45>", "`unknown-login`"]


def test_build_announcement_includes_cleanup_and_mentions():
    text = build_announcement(
        "jce-class-rails",
        "a" * 40,
        "b" * 40,
        ["<@U03M4FZ6P45>"],
        "U02HT4EU4VD",
    )

    assert "`jce-class-rails`" in text
    assert "aaaaaaa" in text and "bbbbbbb" in text
    assert LOCAL_CLEANUP_COMMAND in text
    assert "<@U02HT4EU4VD>" in text
    assert "<@U03M4FZ6P45>" in text


def test_build_announcement_omits_mention_line_when_no_open_pr():
    text = build_announcement("jce-class-rails", "a" * 40, "b" * 40, [], None)

    assert "열린 PR" not in text
    assert "요청:" not in text
