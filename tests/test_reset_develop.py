"""develop 초기화의 담당자 탐색과 안내문 생성 테스트"""

from unittest.mock import MagicMock, patch

from scripts.reset_develop import (
    LOCAL_CLEANUP_COMMAND,
    UnmentionedPull,
    build_announcement,
    build_open_pull_report,
    get_assignee_emails,
    get_open_pulls,
    get_open_pull_urls,
    get_pull_author_emails,
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


def _pull(
    number: int, url: str, author_login: str, author_email: str | None
) -> MagicMock:
    pull = MagicMock()
    pull.number = number
    pull.html_url = url
    pull.user.login = author_login
    commit = MagicMock()
    commit.commit.author.email = author_email
    pull.get_commits.return_value = [commit] if author_email is not None else []
    return pull


def test_get_open_pulls():
    repo = MagicMock()
    pull = MagicMock()
    repo.get_pulls.return_value = [pull]

    assert get_open_pulls(repo) == [pull]
    repo.get_pulls.assert_called_once_with(state="open")


def test_get_open_pull_urls():
    pull = MagicMock()
    pull.number = 1851
    pull.html_url = "https://github.com/team-monolith-product/jce-class-rails/pull/1851"

    assert get_open_pull_urls([pull]) == {1851: pull.html_url}


def test_get_pull_author_emails_uses_first_commit_author_email():
    pull = _pull(1851, "https://example.com/1851", "octocat", "octocat@team-mono.com")

    assert get_pull_author_emails([pull]) == {1851: "octocat@team-mono.com"}


def test_get_pull_author_emails_is_none_without_commits():
    pull = _pull(1851, "https://example.com/1851", "octocat", None)

    assert get_pull_author_emails([pull]) == {1851: None}


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
    text = build_announcement("jce-class-rails", ["<@U0859TEQNRJ>"], [], "U02HT4EU4VD")

    assert "`jce-class-rails`" in text
    assert LOCAL_CLEANUP_COMMAND in text
    assert "<@U02HT4EU4VD>" in text
    assert "<@U0859TEQNRJ>" in text


def test_build_announcement_omits_empty_sections():
    text = build_announcement("jce-class-rails", [], [], None)

    assert "열린 PR" not in text
    assert "요청:" not in text
    assert "담당자를 찾지 못한" not in text


def test_build_announcement_lists_unmentioned_pulls_and_requests_info():
    text = build_announcement(
        "jce-class-rails",
        [],
        [UnmentionedPull(1848, "https://example.com/1848", "octocat")],
        None,
    )

    assert "담당자를 찾지 못한" in text
    assert "노션 작업" in text
    assert "#1848" in text
    assert "https://example.com/1848" in text
    assert "octocat" in text


def test_build_open_pull_report_falls_back_to_pull_author_without_task():
    """작업 미연결 PR 도 작성자 이메일로 슬랙 사용자를 찾으면 멘션된다"""
    pull = _pull(1848, "https://example.com/1848", "octocat", "octocat@team-mono.com")
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [_pr_page(1848, pull.html_url, [])],
        "has_more": False,
    }

    mentions, unmentioned = build_open_pull_report(
        [pull], notion, {"octocat@team-mono.com": "U0ASBJC2SNA"}
    )

    assert mentions == ["<@U0ASBJC2SNA>"]
    assert unmentioned == []


def test_build_open_pull_report_lists_pull_when_no_mention_found():
    """작업도 연결 안 되고 작성자 이메일도 슬랙 계정이 없으면 미해결 목록에 남는다"""
    pull = _pull(1848, "https://example.com/1848", "octocat", "octocat@gmail.com")
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [_pr_page(1848, pull.html_url, [])],
        "has_more": False,
    }

    mentions, unmentioned = build_open_pull_report([pull], notion, {})

    assert mentions == []
    assert unmentioned == [UnmentionedPull(1848, pull.html_url, "octocat")]


def test_main_stops_when_bot_cannot_push(monkeypatch):
    """푸시 권한이 없으면 ref 를 건드리지 않고 이유를 돌려준다"""
    monkeypatch.setenv("GITHUB_TOKEN", "test-dummy-token")
    repo = MagicMock()
    repo.permissions.push = False

    with patch("scripts.reset_develop.Github") as github:
        github.return_value.get_repo.return_value = repo
        result = main("enk-opencode")

    assert "푸시 권한이 없습니다" in result
    repo.get_git_ref.return_value.edit.assert_not_called()
