import os
import datetime
import subprocess
from dotenv import load_dotenv
from github import Github
from github.PullRequest import PullRequest
from markdown import markdown
from weasyprint import HTML, CSS

# 환경 변수 로드
load_dotenv()

# 설정: 토큰, 리포지토리 정보 등
REPO_OWNER = "team-monolith-product"
REPO_NAME = "ped-terraform"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# PyGithub 초기화
g = Github(GITHUB_TOKEN)
repo = g.get_repo(f"{REPO_OWNER}/{REPO_NAME}")

# 6개월 전 날짜 계산
six_months_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
    days=180
)

# PR 리스트 조회: closed 상태의 PR들을 가져온 후, merged 여부와 생성일로 필터링
pulls = repo.get_pulls(state="closed", sort="created", direction="desc")
filtered_pulls = [
    pr for pr in pulls if pr.merged_at is not None and pr.created_at >= six_months_ago
]


def fetch_comments(pr: PullRequest):
    """
    주어진 PR에 대해, 이슈 코멘트와 리뷰 코멘트를 가져오며,
    봇(Bot)이 작성한 코멘트는 필터링합니다.
    """
    issue_comments = pr.get_issue_comments()
    review_comments = pr.get_review_comments()
    reviews = pr.get_reviews()

    # 봇이 작성한 코멘트 제거 (user의 type이 'Bot'인 경우)
    issue_comments = [c for c in issue_comments if c.user and c.user.type != "Bot"]
    review_comments = [c for c in review_comments if c.user and c.user.type != "Bot"]
    reviews = [r for r in reviews if r.user and r.user.type != "Bot"]

    return issue_comments, review_comments, reviews


def generate_markdown(pr, issue_comments, review_comments, reviews):
    """
    PR의 본문과 코멘트들을 읽기 쉽게 마크다운 형식으로 작성합니다.
    """
    lines = []
    lines.append(f"# PR #{pr.number} - {pr.title}")
    lines.append(f"**생성일:** {pr.created_at.isoformat()}")
    lines.append("\n## 본문\n")
    lines.append(pr.body if pr.body else "_본문이 없습니다._")

    lines.append("\n## 이슈 코멘트\n")
    if issue_comments:
        for comment in issue_comments:
            lines.append(
                f"**{comment.user.name}** ({comment.created_at.isoformat()}):\n\n{comment.body}"
            )
            lines.append("\n---")
    else:
        lines.append("_이슈 코멘트가 없습니다._")

    lines.append("\n## 리뷰 코멘트\n")
    if review_comments:
        for comment in review_comments:
            lines.append(
                f"**{comment.user.name}** ({comment.created_at.isoformat()}):\n\n{comment.body}"
            )
            lines.append("\n---")
    else:
        lines.append("_리뷰 코멘트가 없습니다._")

    lines.append("\n## 리뷰\n")
    if reviews:
        for review in reviews:
            lines.append(
                f"**{review.user.name}** ({review.submitted_at.isoformat()}) [{review.state}]:\n\n{review.body}"
            )
            lines.append("\n---")
    else:
        lines.append("_리뷰가 없습니다._")

    return "\n".join(lines)


def convert_markdown_to_pdf(markdown_content, pdf_file):
    raw_html = ""
    raw_html = markdown(markdown_content, extensions=["fenced_code"])

    # write html to file
    with open(f"{pdf_file}.html", "w") as f:
        f.write(raw_html)

    # Weasyprint HTML object
    html = HTML(string=raw_html)

    # Generate PDF
    html.write_pdf(pdf_file)

    print(f"PDF 생성 완료: {pdf_file}")


# 각 PR에 대해 markdown 파일 생성 및 PDF 변환
output_dir = "pr_reports"
os.makedirs(output_dir, exist_ok=True)

for pr in filtered_pulls:
    pr_number = pr.number
    created_date = pr.created_at.strftime("%Y-%m-%d")
    issue_comments, review_comments, reviews = fetch_comments(pr)
    markdown_content = generate_markdown(pr, issue_comments, review_comments, reviews)

    pdf_filename = os.path.join(output_dir, f"PR_{pr_number}_{created_date}.pdf")

    # PDF로 변환
    convert_markdown_to_pdf(markdown_content, pdf_filename)
    print(f"생성됨: {pdf_filename}")
