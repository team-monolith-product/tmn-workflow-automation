import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import argparse
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from github import Github
from github.PullRequest import PullRequest
from slack_sdk import WebClient

from service.github import (
    get_github_client,
    fetch_pull_requests_parallel,
    fetch_pr_review_comments_parallel,
    fetch_comment_reactions_parallel,
)

# 환경 변수 로드
load_dotenv()

# 기본 설정
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get(
    "SLACK_CHANNEL_ID", "C08PTUQFDPV"
)  # 피드백을 보낼 채널 ID
ORG_NAME = "team-monolith-product"  # GitHub 조직 이름
DAYS = 7  # 조회할 데이터 기간 (일)
BAD_REVIEW_REACTIONS = ["👎", "-1", "confused"]  # 나쁜 리뷰로 판단할 반응들


def get_active_repos(
    github_client: Github, org_name: str, min_activity_days: int = 30
) -> list:
    """
    주어진 조직에서 최근 활동이 있는 저장소 목록을 가져옵니다.

    Args:
        github_client: GitHub API 클라이언트
        org_name: 조직 이름
        min_activity_days: 최근 활동 기간 (일)

    Returns:
        활성 저장소 목록 (owner/name 형식)
    """
    # 최소 활동 기간 계산
    min_activity_date = datetime.now(timezone.utc) - timedelta(days=min_activity_days)

    # 조직의 모든 저장소 가져오기
    org = github_client.get_organization(org_name)
    all_repos = list(org.get_repos())  # 페이지네이션 완료를 위해 리스트로 변환

    # 최근 활동이 있는 저장소만 필터링
    active_repos = []

    for repo in all_repos:
        if repo.archived:
            continue
        # fork된 저장소는 제외
        if repo.fork:
            continue

        if not repo.private:
            continue

        # 최근 업데이트 확인
        if repo.updated_at >= min_activity_date or repo.pushed_at >= min_activity_date:
            active_repos.append(f"{org_name}/{repo.name}")

    return active_repos


def fetch_all_pr_data(
    github_client: Github, days: int
) -> tuple[list[PullRequest], dict, dict, dict]:
    """
    모든 PR 데이터를 병렬로 한 번에 가져오고,
    각 PR에 대한 리뷰 댓글과 반응 정보까지 함께 사전 로드합니다.

    Args:
        github_client: GitHub API 클라이언트
        days: 조회할 데이터 기간 (일)

    Returns:
        (PR 목록, 저장소별 PR 수 통계, PR ID와 리뷰 댓글을 연결하는 딕셔너리,
         댓글 ID와 반응을 연결하는 딕셔너리)
    """
    # 조직의 활성 저장소 조회
    repositories = get_active_repos(github_client, ORG_NAME, days)

    # 날짜 계산
    since_date = datetime.now(timezone.utc) - timedelta(days=days)

    # service/github의 fetch_pull_requests_parallel 함수 사용
    print(f"저장소 {len(repositories)}개의 PR을 병렬로 로드합니다...")
    repository_to_pull_requests = fetch_pull_requests_parallel(
        github_client, repositories, since_date
    )

    # 저장소별 PR 수 통계 및 PR 목록 생성
    all_pull_requests = []
    repo_stats = {}

    for repo_full_name, prs in repository_to_pull_requests.items():
        if prs:
            all_pull_requests.extend(prs)
            repo_stats[repo_full_name] = len(prs)

    # PR 리뷰 댓글 병렬 로드
    print(f"PR {len(all_pull_requests)}개의 리뷰 댓글을 병렬로 로드합니다...")
    pr_id_to_comments = fetch_pr_review_comments_parallel(all_pull_requests)

    # 모든 댓글 추출
    all_comments = []
    for comments in pr_id_to_comments.values():
        all_comments.extend(comments)

    # 댓글 반응 정보 병렬 로드
    print(f"댓글 {len(all_comments)}개의 반응 정보를 병렬로 로드합니다...")
    comment_id_to_reactions = fetch_comment_reactions_parallel(all_comments)

    return all_pull_requests, repo_stats, pr_id_to_comments, comment_id_to_reactions


def filter_bad_review_comments(comments) -> list[dict]:
    """
    나쁜 리뷰로 표시된 댓글을 필터링합니다.

    GitHub Reaction만을 기준으로 나쁜 리뷰를 식별합니다.

    Args:
        comments: 댓글 데이터 사전 목록

    Returns:
        나쁜 리뷰로 표시된 댓글 목록
    """
    bad_reviews = []

    for comment_data in comments:
        # 부정적 반응이 있는 경우만 필터링
        has_negative_reaction = any(
            len(comment_data["reaction_users"].get(reaction, [])) > 0
            for reaction in BAD_REVIEW_REACTIONS
        )

        if has_negative_reaction:
            bad_reviews.append(comment_data)

    return bad_reviews


def format_slack_message(bad_reviews: list[dict]) -> list[dict]:
    """
    Slack에 전송할 메시지 블록을 생성합니다.

    Args:
        bad_reviews: 나쁜 리뷰로 표시된 댓글 목록

    Returns:
        슬랙 메시지 블록
    """
    # 현재 날짜 정보
    current_date = datetime.now().strftime("%Y-%m-%d")
    days_ago = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")

    if not bad_reviews:
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🛠️ 코드 리뷰 규칙 개선 피드백",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{days_ago}* ~ *{current_date}* 기간 동안\n개발 규칙에 맞지 않는 리뷰가 없습니다. 👍",
                },
            },
        ]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🛠️ 코드 리뷰 규칙 개선 피드백",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{days_ago}* ~ *{current_date}* 기간 동안\n개발 규칙에 맞지 않다고 표시된 리뷰 {len(bad_reviews)}건을 찾았습니다.\n이 피드백은 `coding-rules.md` 개선에 활용할 수 있습니다.",
            },
        },
        {"type": "divider"},
    ]

    # 저장소별로 그룹화
    repo_groups = {}
    for review in bad_reviews:
        repo_name = review["repo_name"]
        if repo_name not in repo_groups:
            repo_groups[repo_name] = []
        repo_groups[repo_name].append(review)

    # 각 저장소별로 블록 추가
    for repo_name, reviews in repo_groups.items():
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{repo_name}*"}}
        )

        for review in reviews:
            pr_link = (
                f"<{review['html_url']}|{review['repo_name']}#{review['pr_number']}>"
            )

            # 반응 이모지와 생성자 표시
            reactions = []
            for reaction_type, users in review["reaction_users"].items():
                if users and reaction_type in [
                    "+1",
                    "-1",
                    "confused",
                    "heart",
                    "laugh",
                    "hooray",
                    "rocket",
                    "eyes",
                ]:
                    emoji = reaction_type
                    if reaction_type == "+1":
                        emoji = "👍"
                    elif reaction_type == "-1":
                        emoji = "👎"
                    elif reaction_type == "confused":
                        emoji = "😕"
                    elif reaction_type == "heart":
                        emoji = "❤️"
                    elif reaction_type == "laugh":
                        emoji = "😄"
                    elif reaction_type == "hooray":
                        emoji = "🎉"
                    elif reaction_type == "rocket":
                        emoji = "🚀"
                    elif reaction_type == "eyes":
                        emoji = "👀"

                    for user in users:
                        reactions.append(f"{emoji} ({user})")

            reactions_text = " ".join(reactions)

            # 리뷰 본문 일부 표시 (너무 길면 자름)
            body_preview = review["body"]
            if len(body_preview) > 200:
                body_preview = body_preview[:200] + "..."

            # 기본 리뷰 내용
            review_text = f"{pr_link} {reactions_text}\n```{body_preview}```"

            # 답글이 있는 경우 추가
            if "replies" in review and review["replies"]:
                reply_texts = []
                for reply in review["replies"]:
                    reply_body = reply["body"]
                    if len(reply_body) > 150:  # 답글은 좀 더 짧게 표시
                        reply_body = reply_body[:150] + "..."
                    reply_texts.append(f"┗ *@{reply['user']}*: {reply_body}")

                # 줄바꿈으로 답글들 연결하여 원본 리뷰에 추가
                review_text += "\n" + "\n".join(reply_texts)

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": review_text,
                    },
                }
            )

        blocks.append({"type": "divider"})

    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "이 피드백을 바탕으로 `coding-rules.md`를 개선하면 팀의 코드 품질과 리뷰 규칙의 정확도를 높일 수 있습니다.",
            },
        }
    )

    return blocks


def send_to_slack(slack_client: WebClient, channel_id: str, blocks: list[dict]) -> dict:
    """
    결과를 Slack에 전송합니다.

    Args:
        slack_client: Slack API 클라이언트
        channel_id: 슬랙 채널 ID
        blocks: 전송할 메시지 블록

    Returns:
        전송된 메시지의 응답 정보
    """
    return slack_client.chat_postMessage(
        channel=channel_id,
        text="AI 코드 리뷰 규칙 개선 피드백",
        blocks=blocks,
    )


def main():
    """
    리뷰 피드백을 수집하고 Slack에 전송합니다.

    --dry-run 옵션이 주어지면 실제 메시지 전송 없이 콘솔에만 출력합니다.
    --days 옵션으로 조회 기간을 지정할 수 있습니다.
    --channel 옵션으로 Slack 채널 ID를 지정할 수 있습니다.
    """
    parser = argparse.ArgumentParser(description="코드 리뷰 피드백 수집")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="메시지를 Slack에 전송하지 않고 콘솔에만 출력합니다",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DAYS,
        help=f"조회할 데이터 기간(일), 기본값: {DAYS}",
    )
    parser.add_argument(
        "--channel",
        type=str,
        default=SLACK_CHANNEL_ID,
        help=f"메시지를 전송할 Slack 채널 ID, 기본값: {SLACK_CHANNEL_ID}",
    )

    args = parser.parse_args()

    days = args.days
    channel_id = args.channel

    github_client = get_github_client()
    slack_client = WebClient(token=SLACK_BOT_TOKEN)

    # 1. 병렬 처리로 PR 데이터와 리뷰 댓글 한 번에 가져오기
    (
        all_pull_requests,
        repo_stats,
        pr_id_to_comments,
        comment_id_to_reactions,
    ) = fetch_all_pr_data(github_client, days)
    print(
        f"활성 저장소 {len(repo_stats)}개에서 최근 {days}일간 PR {len(all_pull_requests)}개를 가져왔습니다."
    )

    # 2. 리뷰 댓글 데이터 통합 및 반응 정보 연결
    all_review_comments = []

    # 원본 댓글 ID를 답글 목록과 매핑하는 사전
    original_id_to_replies = {}

    # 1단계: 모든 답글 찾아서 사전 구성
    for pr in all_pull_requests:
        if pr.id not in pr_id_to_comments:
            continue

        for comment in pr_id_to_comments[pr.id]:
            # 답글인 경우 (in_reply_to_id 속성이 있고 값이 있는 경우)
            if hasattr(comment, "in_reply_to_id") and comment.in_reply_to_id:
                original_id = comment.in_reply_to_id

                # 사전에 원본 ID가 없으면 빈 목록 생성
                if original_id not in original_id_to_replies:
                    original_id_to_replies[original_id] = []

                # 답글 데이터 생성
                reply_data = {
                    "id": comment.id,
                    "body": comment.body,
                    "user": comment.user.login,
                    "created_at": comment.created_at,
                    "updated_at": comment.updated_at,
                    "html_url": comment.html_url,
                }

                # 답글 목록에 추가
                original_id_to_replies[original_id].append(reply_data)

    # 모든 댓글 정보 순회 (실제 데이터 구성)
    for pr in all_pull_requests:
        if pr.id not in pr_id_to_comments:
            continue

        for comment in pr_id_to_comments[pr.id]:
            # 답글인 경우 건너뛰기 (나중에 원본 댓글의 replies에 추가)
            if hasattr(comment, "in_reply_to_id") and comment.in_reply_to_id:
                continue

            # 기본 댓글 정보 구성
            comment_data = {
                "id": comment.id,
                "body": comment.body,
                "user": comment.user.login,
                "created_at": comment.created_at,
                "updated_at": comment.updated_at,
                "pr_id": pr.id,  # PR ID 추가 (답글 검색용)
                "pr_number": pr.number,
                "pr_title": pr.title,
                "repo_name": pr.base.repo.full_name,
                "html_url": comment.html_url,
                "replies": original_id_to_replies.get(
                    comment.id, []
                ),  # 답글 목록 가져오기
            }

            # 반응 생성자 저장용 사전
            reaction_users = {
                "+1": [],
                "-1": [],
                "confused": [],
                "heart": [],
                "laugh": [],
                "hooray": [],
                "rocket": [],
                "eyes": [],
            }

            # 반응 정보 가져오기
            if comment.id in comment_id_to_reactions:
                reactions = comment_id_to_reactions[comment.id]
                # 각 반응별 생성자 정보 추가
                for reaction in reactions:
                    reaction_type = reaction.content
                    if reaction_type in reaction_users:
                        reaction_users[reaction_type].append(reaction.user.login)

            comment_data["reaction_users"] = reaction_users

            all_review_comments.append(comment_data)

    print(f"리뷰 댓글 {len(all_review_comments)}개를 찾았습니다.")

    # 3. 나쁜 리뷰로 표시된 댓글 필터링 (AI 및 인간 리뷰 모두 포함)
    bad_reviews = filter_bad_review_comments(all_review_comments)
    print(f"개발 규칙에 맞지 않다고 표시된 리뷰 {len(bad_reviews)}개를 찾았습니다.")

    # 4. 메시지 구성 및 전송
    blocks = format_slack_message(bad_reviews)

    if args.dry_run:
        print("=== DRY RUN MODE ===")
        print(f"전송할 메시지 블록 수: {len(blocks)}")
        for block in blocks:
            if block.get("type") == "section" and "text" in block:
                print(block["text"].get("text", ""))
        print("=====================")
    else:
        # Slack에 메시지 전송
        response = send_to_slack(slack_client, channel_id, blocks)
        print(f"메시지가 전송되었습니다: {response.get('ts')}")


if __name__ == "__main__":
    main()
