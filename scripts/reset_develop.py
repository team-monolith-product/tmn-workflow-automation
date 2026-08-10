"""
레포의 develop 브랜치를 main 으로 초기화하고 슬랙에 안내합니다.

develop 은 dev 배포 누적 브랜치라 main 의 머지를 자동으로 따라가지 못하고 주기적으로
main 과 어긋난다. 초기화는 develop ref 를 main 커밋으로 강제 이동시키는 것이며, 이
push 가 dev 이미지 빌드를 트리거해 dev 환경이 main 상태로 재배포된다.

fast-forward 가 아니므로 develop ruleset(non_fast_forward)의 bypass 가 필요하다.
GITHUB_TOKEN 계정(github-machine-monolith)은 bypass 대상인 Bot 팀 소속이다.

로컬 develop 은 각자 손으로 정리해야 하므로 초기화 후 안내에서 열린 PR 의 담당자를
멘션한다. 담당자는 PR → 노션 작업 → 담당자 → 이메일 → 슬랙 사용자로 찾는다.

사용법:
    python scripts/reset_develop.py <repo> [--dry-run]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os
from typing import Iterator

import dotenv
from github import Github, UnknownObjectException
from github.Repository import Repository
from notion_client import Client as NotionClient
from slack_sdk import WebClient

from service.slack import get_email_to_user_id

# 환경 변수 로드
dotenv.load_dotenv()

ORG_NAME = "team-monolith-product"
SLACK_CHANNEL_ID = "C04F0S33HCL"  # t_개발

# 노션 GitHub 동기화 DB 의 풀 리퀘스트 데이터 소스
NOTION_PR_DATA_SOURCE_ID = "71fc180e-4700-47b1-957a-a4de4f6cd9bb"

# 노션 필터의 조건 개수 상한
NOTION_FILTER_CHUNK = 100

LOCAL_CLEANUP_COMMAND = (
    "git fetch origin --prune && git switch main && git branch -D develop"
)


def get_open_pull_urls(repo: Repository) -> dict[int, str]:
    """
    열린 PR 의 번호와 URL 을 모읍니다.

    Args:
        repo: 대상 리포지토리

    Returns:
        dict[int, str]: PR 번호 → PR URL
    """
    return {pull.number: pull.html_url for pull in repo.get_pulls(state="open")}


def _query_pr_pages(notion: NotionClient, numbers: list[int]) -> Iterator[dict]:
    """
    노션 PR 데이터 소스에서 주어진 번호의 페이지를 모두 훑습니다.

    Args:
        notion: 노션 클라이언트
        numbers: PR 번호 목록

    Yields:
        dict: 노션 PR 페이지
    """
    for start in range(0, len(numbers), NOTION_FILTER_CHUNK):
        chunk = numbers[start : start + NOTION_FILTER_CHUNK]
        body = {
            "data_source_id": NOTION_PR_DATA_SOURCE_ID,
            "filter": {
                "or": [
                    {"property": "PR Number", "number": {"equals": number}}
                    for number in chunk
                ]
            },
        }
        while True:
            results = notion.data_sources.query(**body)
            yield from results["results"]
            if not results["has_more"]:
                break
            body["start_cursor"] = results["next_cursor"]


def get_task_page_ids(
    notion: NotionClient, pull_urls: dict[int, str]
) -> dict[int, list[str]]:
    """
    PR 마다 연결된 노션 작업 페이지를 찾습니다.

    노션 PR 데이터 소스는 레포로 필터할 수 없고 PR 번호는 레포마다 겹치므로,
    번호로 조회한 뒤 PR URL 이 일치하는 페이지만 남긴다.

    Args:
        notion: 노션 클라이언트
        pull_urls: PR 번호 → PR URL

    Returns:
        dict[int, list[str]]: PR 번호 → 작업 페이지 ID 목록 (연결된 작업이 있는 PR 만)
    """
    task_page_ids: dict[int, list[str]] = {}
    for page in _query_pr_pages(notion, list(pull_urls)):
        number = page["properties"]["PR Number"]["number"]
        if page["properties"]["_external_object_url"]["url"] != pull_urls.get(number):
            continue
        relation = page["properties"]["작업"]["relation"]
        if relation:
            task_page_ids[number] = [related["id"] for related in relation]
    return task_page_ids


def get_assignee_emails(notion: NotionClient, task_page_ids: list[str]) -> list[str]:
    """
    작업 페이지의 담당자 이메일을 중복 없이 모읍니다.

    Args:
        notion: 노션 클라이언트
        task_page_ids: 작업 페이지 ID 목록

    Returns:
        list[str]: 담당자 이메일 목록
    """
    emails: list[str] = []
    for page_id in task_page_ids:
        page = notion.pages.retrieve(page_id=page_id)
        for person in page["properties"]["담당자"]["people"]:
            email = person["person"]["email"]
            if email not in emails:
                emails.append(email)
    return emails


def to_mentions(emails: list[str], email_to_user_id: dict[str, str]) -> list[str]:
    """
    이메일을 슬랙 멘션으로 바꿉니다.

    슬랙 계정이 없는 이메일은 멘션을 만들지 않고 이메일을 그대로 남깁니다.

    Args:
        emails: 담당자 이메일 목록
        email_to_user_id: 이메일 → 슬랙 사용자 ID

    Returns:
        list[str]: 슬랙 멘션 또는 이메일 목록
    """
    return [
        f"<@{email_to_user_id[email]}>" if email in email_to_user_id else email
        for email in emails
    ]


def build_announcement(
    repo_name: str,
    mentions: list[str],
    unlinked_pull_urls: dict[int, str],
    caller_slack_user_id: str | None,
) -> str:
    """
    초기화 완료 안내문을 만듭니다.

    Args:
        repo_name: 레포 이름
        mentions: 열린 PR 담당자의 슬랙 멘션 목록
        unlinked_pull_urls: 노션 작업이 연결되지 않아 담당자를 못 찾은 PR
        caller_slack_user_id: 명령을 실행한 사람

    Returns:
        str: 슬랙 메시지 본문
    """
    requester = f" 요청: <@{caller_slack_user_id}>" if caller_slack_user_id else ""
    lines = [
        f":recycle: `{repo_name}` 의 develop 을 main 으로 초기화했습니다.{requester}",
        "",
        "로컬에 develop 이 있다면 정리해 주세요.",
        f"```{LOCAL_CLEANUP_COMMAND}```",
    ]
    if mentions:
        lines += ["", f"열린 PR 이 있는 분들 {' '.join(mentions)}"]
    if unlinked_pull_urls:
        links = " ".join(
            f"<{url}|#{number}>" for number, url in sorted(unlinked_pull_urls.items())
        )
        lines += ["", f"노션 작업이 없어 담당자를 못 찾은 열린 PR {links}"]
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

    notion = NotionClient(auth=os.environ["NOTION_TOKEN"], notion_version="2025-09-03")
    pull_urls = get_open_pull_urls(repo)
    task_page_ids = get_task_page_ids(notion, pull_urls)
    emails = get_assignee_emails(
        notion, [page_id for ids in task_page_ids.values() for page_id in ids]
    )
    announcement = build_announcement(
        repo_name,
        to_mentions(emails, get_email_to_user_id(slack_client)),
        {n: url for n, url in pull_urls.items() if n not in task_page_ids},
        caller_slack_user_id,
    )

    # 되돌리려면 초기화 전 커밋이 필요하다. force 이동이라 ref 이력이 남지 않는다.
    print(f"develop {old_sha} → {main_sha}")

    if dry_run:
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
