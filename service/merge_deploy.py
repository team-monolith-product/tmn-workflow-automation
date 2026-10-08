import os
import re
import subprocess
import tempfile
from pathlib import Path


def merge(repository: str, issue_number: int, token: str) -> str:
    if not re.fullmatch(r"team-monolith-product/[A-Za-z0-9_.-]+", repository):
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
        for stage, arguments in commands:
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
            logs.append(result.stdout.decode("utf-8", errors="backslashreplace"))
            logs.append(f"\nexit_code={result.returncode}\n")
            if result.returncode:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    command,
                    output="".join(logs).replace(token, "***"),
                )
        logs.append("DEPLOY_STAGE=push-complete\n")
    return "".join(logs).replace(token, "***")
