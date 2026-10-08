import asyncio
import hashlib
import hmac
import json
import re
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from github import GithubException

from app import github_deploy


@pytest.fixture
def payload() -> dict:
    return {
        "action": "created",
        "installation": {"id": 123},
        "repository": {"full_name": "team-monolith-product/example"},
        "issue": {"number": 7, "pull_request": {}},
        "comment": {"id": 88, "body": "/deploy"},
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DEPLOY_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("DEPLOY_APP_CLIENT_ID", "test-client")
    monkeypatch.setenv("DEPLOY_APP_PRIVATE_KEY", "test-key")
    app = FastAPI()
    app.include_router(github_deploy.router)
    return TestClient(app)


def send(client: TestClient, payload: dict | list, event: str = "issue_comment"):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return client.post(
        "/github/deploy",
        content=body,
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signature},
    )


def test_signed_request_executes_without_legacy_credentials(
    client, payload, monkeypatch
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    execute = Mock()
    monkeypatch.setattr(github_deploy, "execute", execute)
    response = send(client, payload)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    execute.assert_called_once_with(
        123,
        {
            "repository": "team-monolith-product/example",
            "issue_number": "7",
            "comment_id": "88",
        },
    )


def test_invalid_signature_never_executes(client, payload, monkeypatch):
    execute = Mock()
    monkeypatch.setattr(github_deploy, "execute", execute)
    response = client.post("/github/deploy", json=payload)
    assert response.status_code == 401
    execute.assert_not_called()


@pytest.mark.parametrize("body", [" /deploy", "/deploy\n", "/deploy staging", "hello"])
def test_only_exact_command_is_accepted(payload, body):
    payload["comment"]["body"] = body
    assert github_deploy.deploy_request("issue_comment", payload) is None


@pytest.mark.parametrize(
    "body", ["/deploy", "/deploy\n", "/deploy staging", "/deployfoo"]
)
def test_jlext_accepts_command_prefix(payload, body):
    payload["repository"]["full_name"] = github_deploy.JLEXT_REPOSITORY
    payload["comment"]["body"] = body
    assert github_deploy.deploy_request("issue_comment", payload) is not None


def test_jlext_success_reacts_with_thumbs_up(monkeypatch, payload):
    payload["repository"]["full_name"] = github_deploy.JLEXT_REPOSITORY
    app, issue = mock_github(monkeypatch)
    monkeypatch.setattr(github_deploy, "merge", Mock(return_value=""))
    github_deploy.execute(123, github_deploy.deploy_request("issue_comment", payload))
    issue.get_comment.return_value.create_reaction.assert_called_once_with("+1")


@pytest.mark.parametrize("change", ["edited", "issue", "foreign", "event"])
def test_unrelated_events_are_ignored(payload, change):
    event = "issue_comment"
    if change == "edited":
        payload["action"] = "edited"
    elif change == "issue":
        payload["issue"].pop("pull_request")
    elif change == "foreign":
        payload["repository"]["full_name"] = "elsewhere/example"
    else:
        event = "push"
    assert github_deploy.deploy_request(event, payload) is None


@pytest.mark.parametrize(
    "invalid", [[], {"comment": None}, {"installation": {"id": "123"}}]
)
def test_invalid_payload_returns_bad_request(client, payload, invalid):
    if isinstance(invalid, dict):
        payload.update(invalid)
    else:
        payload = invalid
    assert send(client, payload).status_code == 400


def test_large_unicode_logs_are_lossless_and_collapsed():
    logs = ("한글 오류 <> ``` ````\n" * 10000) + "last-line"
    comments = github_deploy.failure_comments(
        logs, "요청 댓글 88", "https://example.com", "충돌", "충돌 해결"
    )
    recovered = []
    for body in comments:
        assert len(body.encode("utf-8")) <= 60000
        assert "<details>" in body
        match = re.search(r"\n(`{3,})text\n(.*)\n\1\n", body, re.DOTALL)
        assert match
        recovered.append(match[2])
    assert "".join(recovered) == logs
    assert sum("**`/deploy` 실패**" in body for body in comments) == 1


def test_push_complete_takes_priority_over_notification_error():
    reason, action = github_deploy.failure_reason(
        "DEPLOY_STAGE=push-complete\nHTTP 403"
    )
    assert "push 이후" in reason
    assert "이미 push" in action


def test_execution_uses_scoped_app_token_and_reacts(monkeypatch, payload):
    inputs = github_deploy.deploy_request("issue_comment", payload)
    app, issue = mock_github(monkeypatch)
    merge = Mock(return_value="DEPLOY_STAGE=push-complete\n")
    monkeypatch.setattr(github_deploy, "merge", merge)
    github_deploy.execute(123, inputs)
    app.requester.requestJsonAndCheck.assert_called_once_with(
        "POST",
        "/app/installations/123/access_tokens",
        input={
            "repositories": ["example"],
            "permissions": {
                "contents": "write",
                "workflows": "write",
                "pull_requests": "write",
            },
        },
    )
    merge.assert_called_once_with("team-monolith-product/example", 7, "app-token")
    issue.get_comment.assert_called_once_with(88)
    issue.get_comment.return_value.create_reaction.assert_called_once_with("rocket")


def mock_github(monkeypatch):
    app = Mock()
    client = Mock()
    integration = Mock()
    integration.__enter__ = Mock(return_value=app)
    integration.__exit__ = Mock(return_value=False)
    connection = Mock()
    connection.__enter__ = Mock(return_value=client)
    connection.__exit__ = Mock(return_value=False)
    app.requester.requestJsonAndCheck.return_value = ({}, {"token": "app-token"})
    app.get_app.return_value.slug = "deploy-app"
    issue = client.get_repo.return_value.get_issue.return_value
    issue.get_comments.return_value = []
    monkeypatch.setattr(github_deploy, "app_auth", Mock())
    monkeypatch.setattr(
        github_deploy, "GithubIntegration", Mock(return_value=integration)
    )
    monkeypatch.setattr(github_deploy, "Github", Mock(return_value=connection))
    return app, issue


@pytest.mark.parametrize("phase", ["mint", "merge", "reaction"])
def test_failures_report_all_available_output(monkeypatch, payload, phase):
    inputs = github_deploy.deploy_request("issue_comment", payload)
    error = GithubException(403, {"message": "permission denied"}, None)
    app, issue = mock_github(monkeypatch)
    merge = Mock(return_value="all git output\nDEPLOY_STAGE=push-complete\n")
    report = Mock()
    if phase == "mint":
        app.requester.requestJsonAndCheck.side_effect = error
    elif phase == "merge":
        merge.side_effect = subprocess.CalledProcessError(
            1, ["git"], output="all git output app-token"
        )
    else:
        issue.get_comment.return_value.create_reaction.side_effect = error
    monkeypatch.setattr(github_deploy, "merge", merge)
    monkeypatch.setattr(github_deploy, "report_failure", report)
    github_deploy.execute(123, inputs)
    details = report.call_args.args[2]
    assert "app-token" not in details
    if phase != "mint":
        assert "all git output" in details
    if phase != "merge":
        assert "permission denied" in details


@pytest.fixture
def git_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original_run = subprocess.run
    work = tmp_path / "source"
    work.mkdir()
    origin = tmp_path / "origin.git"

    def git(*arguments: str) -> str:
        result = original_run(
            ["git", *arguments], cwd=work, capture_output=True, check=True
        )
        return result.stdout.decode().strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (work / "file.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("init", "--bare", str(origin))
    git("remote", "add", "origin", str(origin))
    git("push", "origin", "main")
    git("checkout", "-b", "develop")
    git("push", "origin", "develop")
    base = git("rev-parse", "main")

    def local_remote(arguments, **kwargs):
        arguments = list(arguments)
        if "remote" in arguments and "add" in arguments:
            arguments[-1] = str(origin)
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(github_deploy.subprocess, "run", local_remote)
    return work, git, base


def test_real_merge_preserves_main_and_creates_no_ff_commit(git_repository):
    work, git, base = git_repository
    git("checkout", "-b", "feature", "main")
    (work / "feature.txt").write_text("new feature\n")
    git("add", ".")
    git("commit", "-m", "feature")
    head = git("rev-parse", "HEAD")
    git("push", "origin", "HEAD:refs/pull/7/head")
    logs = github_deploy.merge("team-monolith-product/example", 7, "app-token")
    git("fetch", "origin", "develop")
    assert git("show", "-s", "--format=%P", "FETCH_HEAD") == f"{base} {head}"
    assert git("show", "-s", "--format=%s", "FETCH_HEAD") == "Merge PR #7 to develop"
    assert (
        git("show", "-s", "--format=%an <%ae>", "FETCH_HEAD")
        == "github-actions[bot] <github-actions[bot]@users.noreply.github.com>"
    )
    assert git("ls-remote", "origin", "refs/heads/main").split()[0] == base
    assert "DEPLOY_STAGE=push-complete" in logs
    assert "app-token" not in logs
    github_deploy.merge("team-monolith-product/example", 7, "app-token")
    assert git("ls-remote", "origin", "refs/heads/develop").split()[0] == git(
        "rev-parse", "FETCH_HEAD"
    )


def test_real_conflict_leaves_develop_unchanged(git_repository):
    work, git, base = git_repository
    (work / "file.txt").write_text("develop\n")
    git("add", ".")
    git("commit", "-m", "develop change")
    develop_head = git("rev-parse", "HEAD")
    git("push", "origin", "develop")
    git("checkout", "-b", "feature", "main")
    (work / "file.txt").write_text("feature\n")
    git("add", ".")
    git("commit", "-m", "feature change")
    git("push", "origin", "HEAD:refs/pull/7/head")
    with pytest.raises(subprocess.CalledProcessError) as caught:
        github_deploy.merge("team-monolith-product/example", 7, "app-token")
    assert "CONFLICT (" in caught.value.output
    assert "develop 가져오기" in caught.value.output
    assert "PR 병합" in caught.value.output
    assert "develop push" not in caught.value.output
    assert git("ls-remote", "origin", "refs/heads/develop").split()[0] == develop_head


@pytest.mark.parametrize(
    "repository,files,resolves",
    [
        (github_deploy.JLEXT_REPOSITORY, ["package.json"], True),
        (github_deploy.JLEXT_REPOSITORY, ["file.txt"], False),
        (github_deploy.JLEXT_REPOSITORY, ["package.json", "file.txt"], False),
        ("team-monolith-product/example", ["package.json"], False),
    ],
)
def test_real_package_conflict_resolution_is_limited_to_jlext(
    git_repository, repository, files, resolves
):
    work, git, base = git_repository
    (work / "package.json").write_text('{"version":"base"}\n')
    git("add", ".")
    git("commit", "-m", "package base")
    git("branch", "-f", "main", "HEAD")
    for name in files:
        (work / name).write_text('{"version":"develop"}\n')
    git("add", ".")
    git("commit", "-m", "develop changes")
    develop_head = git("rev-parse", "HEAD")
    git("push", "origin", "develop")
    git("checkout", "-b", "feature", "main")
    for name in files:
        (work / name).write_text('{"version":"feature"}\n')
    git("add", ".")
    git("commit", "-m", "feature changes")
    head = git("rev-parse", "HEAD")
    git("push", "origin", "HEAD:refs/pull/7/head")
    if resolves:
        logs = github_deploy.merge(repository, 7, "app-token")
        git("fetch", "origin", "develop")
        assert git("show", "FETCH_HEAD:package.json") == '{"version":"feature"}'
        assert (
            git("show", "-s", "--format=%P", "FETCH_HEAD") == f"{develop_head} {head}"
        )
        assert git("show", "-s", "--format=%s", "FETCH_HEAD") == (
            "Merge PR #7 to develop (resolve package.json)"
        )
        assert "DEPLOY_STAGE=push-complete" in logs
    else:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            github_deploy.merge(repository, 7, "app-token")
        assert "CONFLICT (" in caught.value.output
        assert "develop push" not in caught.value.output
        if repository == github_deploy.JLEXT_REPOSITORY:
            assert "충돌 파일 확인" in caught.value.output
            assert "병합 취소" in caught.value.output
        assert (
            git("ls-remote", "origin", "refs/heads/develop").split()[0] == develop_head
        )


def test_timeout_preserves_partial_output_and_redacts_token(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], 180, output=b"partial output app-token"
        )

    monkeypatch.setattr(github_deploy.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        github_deploy.merge("team-monolith-product/example", 7, "app-token")
    assert "partial output ***" in caught.value.output
    assert "180초 제한" in caught.value.output


def test_concurrent_push_is_rejected_without_overwriting_develop(
    git_repository, monkeypatch
):
    work, git, base = git_repository
    git("checkout", "-b", "feature", "main")
    (work / "feature.txt").write_text("feature\n")
    git("add", ".")
    git("commit", "-m", "feature")
    git("push", "origin", "HEAD:refs/pull/7/head")
    local_remote = github_deploy.subprocess.run
    concurrent_head = []

    def concurrent_push(arguments, **kwargs):
        if arguments[-3:] == ["push", "origin", "develop"]:
            git("checkout", "develop")
            (work / "concurrent.txt").write_text("concurrent\n")
            git("add", ".")
            git("commit", "-m", "concurrent")
            git("push", "origin", "develop")
            concurrent_head.append(git("rev-parse", "HEAD"))
        return local_remote(arguments, **kwargs)

    monkeypatch.setattr(github_deploy.subprocess, "run", concurrent_push)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        github_deploy.merge("team-monolith-product/example", 7, "app-token")
    assert "[rejected]" in caught.value.output
    assert "develop push" in caught.value.output
    assert (
        git("ls-remote", "origin", "refs/heads/develop").split()[0]
        == concurrent_head[0]
    )


def test_missing_develop_is_reported_with_git_output(git_repository):
    work, git, base = git_repository
    git("push", "origin", "--delete", "develop")
    with pytest.raises(subprocess.CalledProcessError) as caught:
        github_deploy.merge("team-monolith-product/example", 7, "app-token")
    assert "couldn't find remote ref refs/heads/develop" in caught.value.output
    assert (
        "develop 브랜치가 없습니다"
        in github_deploy.failure_reason(caught.value.output)[0]
    )


@pytest.mark.asyncio
async def test_response_and_event_loop_remain_available_during_git_work(
    payload, monkeypatch
):
    finished = threading.Event()
    responded = asyncio.Event()
    monkeypatch.setenv("DEPLOY_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("DEPLOY_APP_CLIENT_ID", "test-client")
    monkeypatch.setenv("DEPLOY_APP_PRIVATE_KEY", "test-key")
    monkeypatch.setattr(github_deploy, "execute", lambda *args: finished.wait(5))
    app = FastAPI()
    app.include_router(github_deploy.router)
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/github/deploy",
        "query_string": b"",
        "headers": [
            (b"x-github-event", b"issue_comment"),
            (b"x-hub-signature-256", signature.encode()),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": body}

    async def send(message):
        if message["type"] == "http.response.body":
            assert json.loads(message["body"]) == {"status": "accepted"}
            responded.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(responded.wait(), 1)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        finished.set()
        await task


def test_failure_report_reuses_own_comment_on_redelivery(payload, monkeypatch):
    inputs = github_deploy.deploy_request("issue_comment", payload)
    app, issue = mock_github(monkeypatch)
    existing = []
    issue.get_comments.return_value = existing

    def create_comment(body):
        comment = Mock(body=body)
        comment.user.login = "deploy-app[bot]"
        existing.append(comment)

    issue.create_comment.side_effect = create_comment
    github_deploy.report_failure(123, inputs, "CONFLICT (content)\ncomplete output")
    github_deploy.report_failure(123, inputs, "CONFLICT (content)\ncomplete output")
    issue.create_comment.assert_called_once()
    assert "충돌" in existing[0].body
    assert "complete output" in existing[0].body


def test_git_can_execute_askpass_and_read_token(git_repository, monkeypatch):
    work, git, base = git_repository
    git("push", "origin", "HEAD:refs/pull/7/head")
    local_remote = github_deploy.subprocess.run
    checked = []

    def check_askpass(arguments, **kwargs):
        if arguments[-1] == "init":
            result = local_remote(
                ["git", "credential", "fill"],
                input=b"protocol=https\nhost=example.invalid\n\n",
                **kwargs,
            )
            assert result.returncode == 0
            assert b"username=x-access-token" in result.stdout
            assert b"password=app-token" in result.stdout
            checked.append(True)
        return local_remote(arguments, **kwargs)

    monkeypatch.setattr(github_deploy.subprocess, "run", check_askpass)
    github_deploy.merge("team-monolith-product/example", 7, "app-token")
    assert checked == [True]
