import os
import argparse
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from github import Github
from github.PullRequest import PullRequest
from slack_sdk import WebClient

# 환경 변수 로드
load_dotenv()

# 기본 설정
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C086HAVUFR8")  # 피드백을 보낼 채널 ID
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


def fetch_pull_requests(
    github_client: Github, repo_owner: str, repo_name: str, days: int
) -> list[PullRequest]:
    """
    주어진 기간 동안의 PR을 가져옵니다.
    """
    # 날짜 계산
    since_date = datetime.now(timezone.utc) - timedelta(days=days)

    # 저장소 접근
    repo = github_client.get_repo(f"{repo_owner}/{repo_name}")

    # PR 조회: 모든 상태의 PR을 일괄로 가져옴
    all_pulls = []

    # 제한 없이 모든 기간 내 PR을 가져옴
    MAX_PRS_PER_REPO = 100  # 충분히 높은 값으로 설정
    pr_count = 0

    # 모든 PR을 업데이트 날짜 기준 내림차순으로 가져옴 (가장 최근 항목부터)
    # state="all"로 open과 closed PR을 한 번에 가져옴
    all_prs_iterator = repo.get_pulls(state="all", sort="updated", direction="desc")

    # 필요한 만큼만 가져오기 - 페이지네이션 최소화
    for pr in all_prs_iterator:
        # 날짜가 범위를 벗어나면 중단 (업데이트 순으로 정렬되어 있으므로 최적화 가능)
        if pr.updated_at < since_date and pr.created_at < since_date:
            break

        # PR을 결과 목록에 추가
        all_pulls.append(pr)
        pr_count += 1

        # 최대 개수에 도달하면 중단
        if pr_count >= MAX_PRS_PER_REPO:
            break

    return all_pulls


def get_pr_review_comments(pr: PullRequest) -> list[dict]:
    """
    PR의 모든 리뷰 댓글을 가져옵니다.

    Args:
        pr: 풀 리퀘스트 객체

    Returns:
        리뷰 댓글 목록
    """
    # PR의 모든 리뷰 댓글 가져오기
    review_comments = pr.get_review_comments()
    
    comments = []
    for comment in review_comments:
        # GitHub API에서 PullRequestComment는 반응 정보가 직접 제공되지 않습니다.
        # GitHub REST API를 통해 각 댓글의 반응을 가져와야 합니다.
        
        # 실제 반응 정보를 가져오기 위해 GitHub API를 직접 호출해야 함
        # 현재는 빈 반응 데이터로 설정
        reactions = {
            "+1": 0,
            "-1": 0,
            "confused": 0,
            "heart": 0,
            "laugh": 0,
            "hooray": 0,
            "rocket": 0,
            "eyes": 0
        }
        
        # GitHub API를 통해 리뷰 댓글의 반응 정보를 가져와야 함
        # GET /repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions
        
        comments.append({
            "id": comment.id,
            "body": comment.body,
            "user": comment.user.login,
            "created_at": comment.created_at,
            "updated_at": comment.updated_at,
            "reactions": reactions,
            "pr_number": pr.number,
            "pr_title": pr.title,
            "repo_name": pr.base.repo.full_name,
            "html_url": comment.html_url
        })
    
    return comments


def filter_ai_review_comments(comments: list[dict]) -> list[dict]:
    """
    AI 리뷰 댓글을 필터링합니다.

    Args:
        comments: 모든 리뷰 댓글 목록

    Returns:
        AI 리뷰 댓글 목록
    """
    ai_comments = []
    
    # AI 리뷰어 식별을 위한 패턴 (여러 패턴을 정의하여 다양한 AI 봇 감지)
    ai_patterns = [
        "github-actions", 
        "bot", 
        "ai-code-reviewer",
        "codecov",
        "dependabot",
        "stale"
    ]
    
    for comment in comments:
        # 사용자 이름에 AI 패턴이 포함되어 있는지 확인
        user_name = comment["user"].lower()
        if any(pattern in user_name for pattern in ai_patterns):
            ai_comments.append(comment)
            continue
            
        # 댓글 내용에 AI에 의해 생성되었다는 표시가 있는지 확인
        body = comment["body"].lower()
        ai_content_patterns = [
            "i'm a bot", 
            "i am a bot", 
            "automated review", 
            "ai review",
            "ai detected",
            "automatic analysis"
        ]
        
        if any(pattern in body for pattern in ai_content_patterns):
            ai_comments.append(comment)
    
    return ai_comments


def filter_bad_review_comments(comments: list[dict]) -> list[dict]:
    """
    나쁜 리뷰로 표시된 댓글을 필터링합니다.
    
    GitHub Reaction만을 기준으로 나쁜 리뷰를 식별합니다.
    (실제 구현에서는 GitHub API를 통해 반응을 가져와야 합니다)

    Args:
        comments: 모든 리뷰 댓글 목록

    Returns:
        나쁜 리뷰로 표시된 댓글 목록
    """
    bad_reviews = []
    
    for comment in comments:
        # 부정적 반응이 있는 경우만 필터링
        has_negative_reaction = any(
            comment["reactions"].get(reaction, 0) > 0 
            for reaction in BAD_REVIEW_REACTIONS
        )
        
        if has_negative_reaction:
            bad_reviews.append(comment)
    
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
    current_date = datetime.now().strftime('%Y-%m-%d')
    days_ago = (datetime.now() - timedelta(days=DAYS)).strftime('%Y-%m-%d')
    
    if not bad_reviews:
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🛠️ 코드 리뷰 규칙 개선 피드백", "emoji": True}
            },
            {
                "type": "section", 
                "text": {"type": "mrkdwn", "text": f"*{days_ago}* ~ *{current_date}* 기간 동안\n개발 규칙에 맞지 않는 리뷰가 없습니다. 👍"}
            }
        ]
    
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🛠️ 코드 리뷰 규칙 개선 피드백", "emoji": True}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{days_ago}* ~ *{current_date}* 기간 동안\n개발 규칙에 맞지 않다고 표시된 리뷰 {len(bad_reviews)}건을 찾았습니다.\n이 피드백은 `coding-rules.md` 개선에 활용할 수 있습니다."}
        },
        {"type": "divider"}
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
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{repo_name}*"}
        })
        
        for review in reviews:
            pr_link = f"<{review['html_url']}|{review['repo_name']}#{review['pr_number']}>"
            
            # 반응 이모지 표시
            reactions = []
            for reaction_type, count in review["reactions"].items():
                if count > 0 and reaction_type in ["+1", "-1", "confused", "heart", "laugh", "hooray", "rocket", "eyes"]:
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
                    
                    reactions.append(f"{emoji}{count}")
            
            reactions_text = " ".join(reactions)
            
            # 리뷰 본문 일부 표시 (너무 길면 자름)
            body_preview = review["body"]
            if len(body_preview) > 200:
                body_preview = body_preview[:200] + "..."
            
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{pr_link} {reactions_text}\n```{body_preview}```"}
            })
        
        blocks.append({"type": "divider"})
    
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "이 피드백을 바탕으로 `coding-rules.md`를 개선하면 팀의 코드 품질과 리뷰 규칙의 정확도를 높일 수 있습니다."}
    })
    
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

    github_client = Github(GITHUB_TOKEN)
    slack_client = WebClient(token=SLACK_BOT_TOKEN)

    # 1. 활성 저장소 목록 가져오기
    repositories = get_active_repos(github_client, ORG_NAME, days)
    print(f"활성 저장소 {len(repositories)}개를 찾았습니다.")

    # 2. 각 저장소에서 PR 가져오기
    all_pull_requests = []
    for repo_full_name in repositories:
        repo_owner, repo_name = repo_full_name.split("/")
        repo_prs = fetch_pull_requests(github_client, repo_owner, repo_name, days)
        all_pull_requests.extend(repo_prs)
    
    print(f"최근 {days}일간 PR {len(all_pull_requests)}개를 가져왔습니다.")

    # 3. 모든 PR에서 리뷰 댓글 가져오기
    all_review_comments = []
    for pr in all_pull_requests:
        pr_comments = get_pr_review_comments(pr)
        all_review_comments.extend(pr_comments)
    
    print(f"리뷰 댓글 {len(all_review_comments)}개를 찾았습니다.")

    # 4. 나쁜 리뷰로 표시된 댓글 필터링 (AI 및 인간 리뷰 모두 포함)
    bad_reviews = filter_bad_review_comments(all_review_comments)
    print(f"개발 규칙에 맞지 않다고 표시된 리뷰 {len(bad_reviews)}개를 찾았습니다.")

    # 6. 메시지 구성 및 전송
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