"""
레포의 develop 브랜치를 main 으로 초기화하고 슬랙에 안내합니다.

develop 은 dev 배포 누적 브랜치라 main 의 머지를 자동으로 따라가지 못하고 주기적으로
main 과 어긋난다. 초기화는 develop ref 를 main 커밋으로 강제 이동시키는 것이며, 이
push 가 dev 이미지 빌드를 트리거해 dev 환경이 main 상태로 재배포된다.

fast-forward 가 아니므로 develop ruleset(non_fast_forward)의 bypass 가 필요하다.
GITHUB_TOKEN 계정(github-machine-monolith)은 bypass 대상인 Bot 팀 소속이다.

로컬 develop 은 각자 손으로 정리해야 하므로 초기화 후 안내에서 열린 PR 작성자를
멘션한다.

사용법:
    python scripts/reset_develop.py <repo> [--dry-run]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os

import dotenv
from github import Github, UnknownObjectException
from github.Repository import Repository
from slack_sdk import WebClient

from service.config import load_config

# 환경 변수 로드
dotenv.load_dotenv()

ORG_NAME = "team-monolith-product"
SLACK_CHANNEL_ID = "C04F0S33HCL"  # t_개발

LOCAL_CLEANUP_COMMAND = (
    "git fetch origin --prune && git switch main && git branch -D develop"
)


def get_open_pr_authors(repo: Repository) -> list[str]:
    """
    열린 PR 작성자의 GitHub 로그인을 등장 순서대로 중복 없이 모읍니다.

    봇 계정은 로컬 브랜치를 들고 있지 않으므로 제외합니다.

    Args:
        repo: 대상 리포지토리

    Returns:
        list[str]: GitHub 로그인 목록
    """
    logins: list[str] = []
    for pull in repo.get_pulls(state="open"):
        login = pull.user.login
        if login.endswith("[bot]") or login in logins:
            continue
        logins.append(login)
    return logins


def to_mentions(logins: list[str]) -> list[str]:
    """
    GitHub 로그인을 슬랙 멘션으로 바꿉니다.

    GitHub 프로필에 회사 이메일이 공개되지 않아 자동 매칭이 불가능하므로
    config.yaml 의 github_slack_users 매핑을 사용합니다.
    매핑에 없으면 멘션을 만들지 않고 로그인을 그대로 남깁니다.

    Args:
        logins: GitHub 로그인 목록

    Returns:
        list[str]: 슬랙 멘션 또는 GitHub 로그인 목록
    """
    github_slack_users = load_config().github_slack_users
    return [
        (
            f"<@{github_slack_users[login]}>"
            if login in github_slack_users
            else f"`{login}`"
        )
        for login in logins
    ]


def build_announcement(
    repo_name: str,
    old_sha: str,
    new_sha: str,
    mentions: list[str],
    caller_slack_user_id: str | None,
) -> str:
    """
    초기화 완료 안내문을 만듭니다.

    Args:
        repo_name: 레포 이름
        old_sha: 초기화 전 develop 커밋
        new_sha: 초기화 후 develop 커밋 (= main)
        mentions: 열린 PR 작성자의 슬랙 멘션 목록
        caller_slack_user_id: 명령을 실행한 사람

    Returns:
        str: 슬랙 메시지 본문
    """
    requester = f" 요청: <@{caller_slack_user_id}>" if caller_slack_user_id else ""
    lines = [
        f":recycle: `{repo_name}` 의 develop 을 main 으로 초기화했습니다.{requester}",
        "",
        f"초기화 전 develop `{old_sha[:7]}` → 현재 develop `{new_sha[:7]}` (main 과 동일)",
        "dev 환경은 곧 main 상태로 재배포됩니다.",
        "",
        "로컬에 develop 이 있다면 정리해 주세요.",
        f"```{LOCAL_CLEANUP_COMMAND}```",
    ]
    if mentions:
        lines += ["", f"열린 PR 이 있는 분들 {' '.join(mentions)}"]
    return "\n".join(lines)


def main(
    repo_name: str = "",
    caller_slack_user_id: str | None = None,
    dry_run: bool = False,
) -> None:
    """
    develop 브랜치를 main 으로 초기화하고 슬랙에 안내합니다.

    Args:
        repo_name: GitHub 레포 이름 (예: jce-class-rails)
        caller_slack_user_id: 명령을 실행한 슬랙 사용자 ID
        dry_run: True 면 초기화와 슬랙 발송 없이 대상만 출력
    """
    slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

    def reply(text: str) -> None:
        """실행한 사람에게만 보이는 응답. 안내가 아니라 명령 결과이므로 공개하지 않는다."""
        print(text)
        if caller_slack_user_id and not dry_run:
            slack_client.chat_postEphemeral(
                channel=SLACK_CHANNEL_ID, user=caller_slack_user_id, text=text
            )

    if not repo_name:
        reply("사용법: `/wa reset-develop <레포 이름>` (예: `jce-class-rails`)")
        return

    github_client = Github(os.environ["GITHUB_TOKEN"])
    try:
        repo = github_client.get_repo(f"{ORG_NAME}/{repo_name}")
        develop_ref = repo.get_git_ref("heads/develop")
    except UnknownObjectException:
        reply(f"`{repo_name}` 레포 또는 그 develop 브랜치를 찾을 수 없습니다.")
        return

    old_sha = develop_ref.object.sha
    main_sha = repo.get_git_ref("heads/main").object.sha
    if old_sha == main_sha:
        reply(f"`{repo_name}` 의 develop 은 이미 main 과 같습니다.")
        return

    mentions = to_mentions(get_open_pr_authors(repo))
    announcement = build_announcement(
        repo_name, old_sha, main_sha, mentions, caller_slack_user_id
    )

    if dry_run:
        print(f"[dry-run] develop {old_sha} → {main_sha}")
        print(announcement)
        return

    develop_ref.edit(sha=main_sha, force=True)
    slack_client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=announcement)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="레포의 develop 브랜치를 main 으로 초기화합니다."
    )
    parser.add_argument("repo", help="GitHub 레포 이름 (예: jce-class-rails)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="초기화와 슬랙 발송 없이 대상만 출력합니다.",
    )
    args = parser.parse_args()
    main(args.repo, dry_run=args.dry_run)
