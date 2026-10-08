import re
import subprocess
from typing import Any

import requests

from api import github
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
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return f"HTTP {error.response.status_code}\n{error.response.text}"
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
    execution: str,
    reason: str | None = None,
    next_action: str | None = None,
) -> None:
    repository = inputs["repository"]
    token = github.get_installation_token(
        installation_id, [repository.split("/")[1]], {"pull_requests": "write"}
    )
    path = f"/repos/{repository}/issues/{inputs['issue_number']}/comments"
    app_slug = github.request("GET", "/app", github.get_app_token()).json()["slug"]
    existing = {
        comment["body"]: comment
        for comment in github.get_pages(path, token)
        if comment["user"]["login"] == f"{app_slug}[bot]"
    }
    default_reason, default_next_action = failure_reason(logs)
    command_url = f"https://github.com/{repository}/pull/{inputs['issue_number']}#issuecomment-{inputs['comment_id']}"
    for body in failure_comments(
        logs,
        execution,
        command_url,
        reason or default_reason,
        next_action or default_next_action,
    ):
        title = body.split("<summary>", 1)[1].split("</summary>", 1)[0]
        previous = next(
            (
                comment
                for text, comment in existing.items()
                if f"<summary>{title}</summary>" in text
            ),
            None,
        )
        if previous is None:
            github.request("POST", path, token, json={"body": body})
        elif previous["body"] != body:
            github.request(
                "PATCH",
                f"/repos/{repository}/issues/comments/{previous['id']}",
                token,
                json={"body": body},
            )
    github.request(
        "POST",
        f"/repos/{repository}/issues/comments/{inputs['comment_id']}/reactions",
        token,
        json={"content": "-1"},
    )


def execute(installation_id: int, inputs: dict[str, str]) -> None:
    logs = ""
    token = ""
    try:
        token = github.get_installation_token(
            installation_id,
            [inputs["repository"].split("/")[1]],
            {"contents": "write", "workflows": "write", "pull_requests": "write"},
        )
        logs = merge(inputs["repository"], int(inputs["issue_number"]), token)
        github.request(
            "POST",
            f"/repos/{inputs['repository']}/issues/comments/{inputs['comment_id']}/reactions",
            token,
            json={"content": "rocket"},
        )
    except Exception as error:
        details = logs + error_details(error)
        if token:
            details = details.replace(token, "***")
        report_failure(
            installation_id,
            inputs,
            details,
            f"요청 댓글 {inputs['comment_id']}",
        )
