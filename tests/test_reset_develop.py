"""develop 초기화의 담당자 탐색과 안내문 생성 테스트"""

from unittest.mock import MagicMock, patch

from scripts.reset_develop import (
    LOCAL_CLEANUP_COMMAND,
    build_announcement,
    get_assignee_emails,
    get_open_pull_urls,
    get_task_page_ids,
    main,
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


def test_build_announcement_includes_cleanup_and_mentions():
    text = build_announcement("jce-class-rails", ["<@U0859TEQNRJ>"], "U02HT4EU4VD")

    assert "`jce-class-rails`" in text
    assert LOCAL_CLEANUP_COMMAND in text
    assert "<@U02HT4EU4VD>" in text
    assert "<@U0859TEQNRJ>" in text


def test_build_announcement_omits_empty_sections():
    text = build_announcement("jce-class-rails", [], None)

    assert "열린 PR" not in text
    assert "요청:" not in text


def test_main_dry_run_does_not_edit_or_announce(monkeypatch):
    repo, develop, slack, github = _reset_clients(monkeypatch)
    with github:
        result = main("tmn-deploy-verification", dry_run=True)
    assert "초기화했습니다" in result
    develop.edit.assert_not_called()
    slack.chat_postMessage.assert_not_called()


def test_main_edits_before_announcement(monkeypatch):
    repo, develop, slack, github = _reset_clients(monkeypatch)
    order = MagicMock()
    order.attach_mock(develop.edit, "edit")
    order.attach_mock(slack.chat_postMessage, "announce")
    with github:
        result = main("tmn-deploy-verification")
    develop.edit.assert_called_once_with(sha="main-sha", force=True)
    assert [call[0] for call in order.mock_calls] == ["edit", "announce"]
    assert "안내했습니다" in result


def test_main_failed_edit_does_not_announce(monkeypatch):
    from github import GithubException

    repo, develop, slack, github = _reset_clients(monkeypatch)
    develop.edit.side_effect = GithubException(
        403, {"message": "Resource not accessible by integration"}
    )
    with github:
        result = main("tmn-deploy-verification")
    assert "GitHub 403" in result
    assert "Resource not accessible by integration" in result
    slack.chat_postMessage.assert_not_called()


def _reset_clients(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-slack-token")
    monkeypatch.setenv("NOTION_TOKEN", "test-notion-token")
    repo = MagicMock()
    develop = MagicMock()
    develop.object.sha = "develop-sha"
    main_ref = MagicMock()
    main_ref.object.sha = "main-sha"
    repo.get_git_ref.side_effect = [develop, main_ref]
    repo.get_pulls.return_value = []
    slack = MagicMock()
    monkeypatch.setattr("scripts.reset_develop.WebClient", lambda **kwargs: slack)
    monkeypatch.setattr("scripts.reset_develop.NotionClient", MagicMock())
    monkeypatch.setattr("scripts.reset_develop.get_email_to_user_id", lambda client: {})
    client = MagicMock()
    client.get_repo.return_value = repo
    github = patch("scripts.reset_develop.get_github_client", return_value=client)
    return repo, develop, slack, github
