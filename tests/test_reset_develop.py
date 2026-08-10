"""develop 초기화의 담당자 탐색과 안내문 생성 테스트"""

from unittest.mock import MagicMock

from scripts.reset_develop import (
    LOCAL_CLEANUP_COMMAND,
    build_announcement,
    get_assignee_emails,
    get_open_pull_urls,
    get_task_page_ids,
    to_mentions,
)


def _pr_page(number: int, url: str, task_ids: list[str]) -> dict:
    return {
        "properties": {
            "PR Number": {"number": number},
            "_external_object_url": {"url": url},
            "작업": {"relation": [{"id": task_id} for task_id in task_ids]},
        }
    }


def test_get_open_pull_urls():
    repo = MagicMock()
    pull = MagicMock()
    pull.number = 1851
    pull.html_url = "https://github.com/team-monolith-product/jce-class-rails/pull/1851"
    repo.get_pulls.return_value = [pull]

    assert get_open_pull_urls(repo) == {1851: pull.html_url}
    repo.get_pulls.assert_called_once_with(state="open")


def test_get_task_page_ids_ignores_other_repos_with_same_number():
    """PR 번호는 레포마다 겹치므로 URL 이 일치하는 페이지만 남아야 한다"""
    ours = "https://github.com/team-monolith-product/jce-class-rails/pull/1851"
    theirs = "https://github.com/team-monolith-product/jce-codle-react/pull/1851"
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [
            _pr_page(1851, theirs, ["other"]),
            _pr_page(1851, ours, ["task-a"]),
        ],
        "has_more": False,
    }

    assert get_task_page_ids(notion, {1851: ours}) == {1851: ["task-a"]}


def test_get_task_page_ids_skips_pull_without_task():
    url = "https://github.com/team-monolith-product/jce-class-rails/pull/1848"
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [_pr_page(1848, url, [])],
        "has_more": False,
    }

    assert get_task_page_ids(notion, {1848: url}) == {}


def test_get_task_page_ids_follows_pagination():
    url = "https://github.com/team-monolith-product/jce-class-rails/pull/1851"
    notion = MagicMock()
    notion.data_sources.query.side_effect = [
        {"results": [], "has_more": True, "next_cursor": "cursor-1"},
        {"results": [_pr_page(1851, url, ["task-a"])], "has_more": False},
    ]

    assert get_task_page_ids(notion, {1851: url}) == {1851: ["task-a"]}
    assert notion.data_sources.query.call_args.kwargs["start_cursor"] == "cursor-1"


def test_get_assignee_emails_deduplicates():
    notion = MagicMock()
    notion.pages.retrieve.return_value = {
        "properties": {
            "담당자": {
                "people": [
                    {"person": {"email": "peko@team-mono.com"}},
                    {"person": {"email": "pky@team-mono.com"}},
                ]
            }
        }
    }

    emails = get_assignee_emails(notion, ["task-a", "task-b"])

    assert emails == ["peko@team-mono.com", "pky@team-mono.com"]


def test_to_mentions_falls_back_to_email_when_no_slack_account():
    mentions = to_mentions(
        ["peko@team-mono.com", "gone@team-mono.com"],
        {"peko@team-mono.com": "U0859TEQNRJ"},
    )

    assert mentions == ["<@U0859TEQNRJ>", "gone@team-mono.com"]


def test_build_announcement_includes_cleanup_mentions_and_unlinked_pulls():
    text = build_announcement(
        "jce-class-rails",
        ["<@U0859TEQNRJ>"],
        {1848: "https://github.com/team-monolith-product/jce-class-rails/pull/1848"},
        "U02HT4EU4VD",
    )

    assert "`jce-class-rails`" in text
    assert LOCAL_CLEANUP_COMMAND in text
    assert "<@U02HT4EU4VD>" in text
    assert "<@U0859TEQNRJ>" in text
    assert "|#1848>" in text


def test_build_announcement_omits_empty_sections():
    text = build_announcement("jce-class-rails", [], {}, None)

    assert "열린 PR" not in text
    assert "요청:" not in text
