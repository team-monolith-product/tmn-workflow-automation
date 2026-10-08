import os
import re
import subprocess
from typing import Any

from github import Auth, Github, GithubException, GithubIntegration
from service.merge_deploy import merge

REPOSITORY_PATTERN = r"team-monolith-product/[A-Za-z0-9_.-]+"


def deploy_request(event: str, payload: dict[str, Any]) -> dict[str, str] | None:
    if (
        event != "issue_comment"
        or payload.get("action") != "created"
        or payload.get("comment", {}).get("body") != "/deploy"
        or "pull_request" not in payload.get("issue", {})
    ):
        return None
    repository = payload["repository"]["full_name"]
    if not re.fullmatch(REPOSITORY_PATTERN, repository):
        return None
    issue_number = payload["issue"]["number"]
    comment_id = payload["comment"]["id"]
    if type(issue_number) is not int or issue_number <= 0:
        raise ValueError("PR 번호가 올바르지 않습니다.")
    if type(comment_id) is not int or comment_id <= 0:
        raise ValueError("댓글 번호가 올바르지 않습니다.")
    return {
        "repository": repository,
        "issue_number": str(issue_number),
        "comment_id": str(comment_id),
    }


def error_details(error: Exception) -> str:
    if isinstance(error, GithubException):
        return f"HTTP {error}"
    if isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        return str(error.output)
    return f"{type(error).__name__}: {error}"


def failure_reason(logs: str) -> tuple[str, str]:
    if "DEPLOY_STAGE=push-complete" in logs:
        return (
            "develop push 이후 결과 알림에서 오류가 발생했습니다.",
            "코드는 이미 push됐습니다. 아래 내역에서 알림 오류를 확인해 주세요.",
        )
    if "CONFLICT (" in logs or "Automatic merge failed" in logs:
        return (
            "develop과 PR 사이에 병합 충돌이 있습니다.",
            "충돌을 해결한 뒤 `/deploy`를 다시 남겨 주세요.",
        )
    if "GH013" in logs or "protected branch" in logs:
        return (
            "develop의 저장소 규칙이 push를 막았습니다.",
            "저장소 규칙과 App 권한을 확인한 뒤 `/deploy`를 다시 남겨 주세요.",
        )
    if "Write access to repository not granted" in logs or "HTTP 403" in logs:
        return (
            "GitHub가 작업에 필요한 권한을 허용하지 않았습니다.",
            "App의 설치 범위와 권한을 확인한 뒤 `/deploy`를 다시 남겨 주세요.",
        )
    if "couldn't find remote ref refs/heads/develop" in logs:
        return (
            "저장소에 develop 브랜치가 없습니다.",
            "develop 브랜치를 준비한 뒤 `/deploy`를 다시 남겨 주세요.",
        )
    return (
        "작업을 완료하지 못했습니다.",
        "아래 전체 내역을 확인한 뒤 `/deploy`를 다시 남겨 주세요.",
    )


def failure_comments(
    logs: str, execution: str, command_url: str, reason: str, next_action: str
) -> list[str]:
    comments: list[str] = []
    remaining = logs or "GitHub에서 실행 로그를 제공하지 않았습니다."
    part = 1
    while remaining:
        size = min(30000, len(remaining))
        while True:
            chunk = remaining[:size]
            fence = "`" * max(
                3, 1 + max((len(m[0]) for m in re.finditer(r"`+", chunk)), default=0)
            )
            title = f"전체 내역 · {execution} · {part}"
            summary = (
                f"**`/deploy` 실패**\n\n{reason}\n\n{next_action}\n\n[요청 댓글]({command_url})\n\n"
                if part == 1
                else ""
            )
            body = f"{summary}<details>\n<summary>{title}</summary>\n\n{fence}text\n{chunk}\n{fence}\n\n</details>"
            if len(body.encode("utf-8")) <= 60000:
                break
            size //= 2
        comments.append(body)
        remaining = remaining[size:]
        part += 1
    return comments


def report_failure(
    installation_id: int,
    inputs: dict[str, str],
    logs: str,
) -> None:
    repository = inputs["repository"]
    reason, next_action = failure_reason(logs)
    command_url = f"https://github.com/{repository}/pull/{inputs['issue_number']}#issuecomment-{inputs['comment_id']}"
    with GithubIntegration(auth=app_auth(), timeout=4) as app:
        token = installation_token(
            app, installation_id, repository, {"pull_requests": "write"}
        )
        login = f"{app.get_app().slug}[bot]"
        with Github(auth=Auth.Token(token), timeout=4) as client:
            issue = client.get_repo(repository, lazy=True).get_issue(
                int(inputs["issue_number"])
            )
            existing = [c for c in issue.get_comments() if c.user.login == login]
            for body in failure_comments(
                logs,
                f"요청 댓글 {inputs['comment_id']}",
                command_url,
                reason,
                next_action,
            ):
                title = body.split("<summary>", 1)[1].split("</summary>", 1)[0]
                previous = next(
                    (c for c in existing if f"<summary>{title}</summary>" in c.body),
                    None,
                )
                if previous is None:
                    issue.create_comment(body)
                elif previous.body != body:
                    previous.edit(body)
            issue.get_comment(int(inputs["comment_id"])).create_reaction("-1")


def app_auth() -> Auth.AppAuth:
    return Auth.AppAuth(
        os.environ["DEPLOY_APP_CLIENT_ID"], os.environ["DEPLOY_APP_PRIVATE_KEY"]
    )


def installation_token(
    app: GithubIntegration,
    installation_id: int,
    repository: str,
    permissions: dict[str, str],
) -> str:
    _, data = app.requester.requestJsonAndCheck(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        input={"repositories": [repository.split("/")[1]], "permissions": permissions},
    )
    return data["token"]


def execute(installation_id: int, inputs: dict[str, str]) -> None:
    logs = ""
    token = ""
    try:
        with GithubIntegration(auth=app_auth(), timeout=4) as app:
            token = installation_token(
                app,
                installation_id,
                inputs["repository"],
                {"contents": "write", "workflows": "write", "pull_requests": "write"},
            )
        logs = merge(inputs["repository"], int(inputs["issue_number"]), token)
        with Github(auth=Auth.Token(token), timeout=4) as client:
            issue = client.get_repo(inputs["repository"], lazy=True).get_issue(
                int(inputs["issue_number"])
            )
            issue.get_comment(int(inputs["comment_id"])).create_reaction("rocket")
    except Exception as error:
        details = logs + error_details(error)
        if token:
            details = details.replace(token, "***")
        report_failure(installation_id, inputs, details)
