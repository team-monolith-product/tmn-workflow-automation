import asyncio
import hashlib
import hmac
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from github import Auth, Github, GithubException, GithubIntegration

REPOSITORY_PATTERN = r"team-monolith-product/[A-Za-z0-9_.-]+"
JLEXT_REPOSITORY = "team-monolith-product/jce-codle-jlext"

router = APIRouter()


@router.post("/github/deploy")
async def receive_deploy(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str = Header(""),
    x_hub_signature_256: str = Header(""),
) -> dict[str, str]:
    secret = os.environ.get("DEPLOY_WEBHOOK_SECRET")
    if not all(
        (
            secret,
            os.environ.get("DEPLOY_APP_CLIENT_ID"),
            os.environ.get("DEPLOY_APP_PRIVATE_KEY"),
        )
    ):
        raise HTTPException(503, "GitHub App이 설정되지 않았습니다.")
    body = await request.body()
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(401, "GitHub webhook 서명이 올바르지 않습니다.")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("객체가 필요합니다.")
        inputs = deploy_request(x_github_event, payload)
        installation_id = payload["installation"]["id"] if inputs else None
        if inputs and (type(installation_id) is not int or installation_id <= 0):
            raise ValueError("설치 번호가 올바르지 않습니다.")
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise HTTPException(400, "GitHub webhook 형식이 올바르지 않습니다.") from error
    if inputs is not None:
        background_tasks.add_task(asyncio.to_thread, execute, installation_id, inputs)
        return {"status": "accepted"}
    return {"status": "ok"}


def deploy_request(event: str, payload: dict[str, Any]) -> dict[str, str] | None:
    if (
        event != "issue_comment"
        or payload.get("action") != "created"
        or "pull_request" not in payload.get("issue", {})
    ):
        return None
    repository = payload["repository"]["full_name"]
    if not re.fullmatch(REPOSITORY_PATTERN, repository):
        return None
    body = payload.get("comment", {}).get("body", "")
    if body != "/deploy" and not (
        repository == JLEXT_REPOSITORY and body.startswith("/deploy")
    ):
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
            issue.get_comment(int(inputs["comment_id"])).create_reaction(
                "+1" if inputs["repository"] == JLEXT_REPOSITORY else "rocket"
            )
    except Exception as error:
        details = logs + error_details(error)
        if token:
            details = details.replace(token, "***")
        report_failure(installation_id, inputs, details)


def merge(repository: str, issue_number: int, token: str) -> str:
    if not re.fullmatch(REPOSITORY_PATTERN, repository):
        raise ValueError("저장소가 올바르지 않습니다.")
    if issue_number <= 0 or not token:
        raise ValueError("PR 번호 또는 인증이 올바르지 않습니다.")
    logs: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        askpass = root / "askpass.sh"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) printf "%s" "x-access-token" ;; *) printf "%s" "$DEPLOY_GIT_TOKEN" ;; esac\n',
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        environment = {
            **os.environ,
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "DEPLOY_GIT_TOKEN": token,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
        }
        work = root / "repository"
        work.mkdir()
        commands = [
            ("저장소 준비", ["init"]),
            (
                "저장소 연결",
                ["remote", "add", "origin", f"https://github.com/{repository}.git"],
            ),
            (
                "develop 가져오기",
                ["fetch", "--no-tags", "origin", "refs/heads/develop"],
            ),
            ("develop 선택", ["checkout", "-b", "develop", "FETCH_HEAD"]),
            ("커밋 작성자", ["config", "user.name", "github-actions[bot]"]),
            (
                "커밋 이메일",
                [
                    "config",
                    "user.email",
                    "github-actions[bot]@users.noreply.github.com",
                ],
            ),
            ("PR 가져오기", ["fetch", "origin", f"pull/{issue_number}/head:pr-branch"]),
            (
                "PR 병합",
                [
                    "merge",
                    "pr-branch",
                    "--no-ff",
                    "-m",
                    f"Merge PR #{issue_number} to develop",
                ],
            ),
            ("develop push", ["push", "origin", "develop"]),
        ]

        def run_git(stage: str, arguments: list[str]) -> str:
            command = ["git", "-c", f"core.hooksPath={os.devnull}", *arguments]
            logs.append(f"===== {stage} =====\n$ git {' '.join(arguments)}\n")
            try:
                result = subprocess.run(
                    command,
                    cwd=work,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=180,
                )
            except subprocess.TimeoutExpired as error:
                logs.append(
                    (error.output or b"").decode("utf-8", errors="backslashreplace")
                )
                logs.append("\n180초 제한을 초과했습니다.\n")
                error.output = "".join(logs).replace(token, "***")
                raise
            except OSError as error:
                logs.append(f"{type(error).__name__}: {error}\n")
                raise subprocess.CalledProcessError(
                    127, command, output="".join(logs).replace(token, "***")
                ) from error
            output = result.stdout.decode("utf-8", errors="backslashreplace")
            logs.append(output)
            logs.append(f"\nexit_code={result.returncode}\n")
            if result.returncode:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    command,
                    output="".join(logs).replace(token, "***"),
                )
            return output

        for stage, arguments in commands:
            try:
                run_git(stage, arguments)
            except subprocess.CalledProcessError as error:
                if repository != JLEXT_REPOSITORY or arguments[0] != "merge":
                    raise
                conflicts = run_git(
                    "충돌 파일 확인", ["diff", "--name-only", "--diff-filter=U"]
                ).strip()
                if conflicts != "package.json":
                    run_git("병합 취소", ["merge", "--abort"])
                    error.output = "".join(logs).replace(token, "***")
                    raise
                run_git("package.json 해결", ["checkout", "--theirs", "package.json"])
                run_git("package.json 선택", ["add", "package.json"])
                run_git(
                    "PR 병합 완료",
                    [
                        "commit",
                        "-m",
                        f"Merge PR #{issue_number} to develop (resolve package.json)",
                    ],
                )
        logs.append("DEPLOY_STAGE=push-complete\n")
    return "".join(logs).replace(token, "***")
