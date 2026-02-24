"""
Workflow Automation FastAPI Server

노션 버튼 클릭 시 Webhook을 받아 plan-md 레포에 PR을 생성하는 경량 FastAPI 서버입니다.
"""

import os
import asyncio
import logging
import time
from datetime import datetime
from typing import Optional, Any, Tuple

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn
from app.common import notion_page_to_markdown
from github import Github, GithubException
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# 로깅 설정
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============================================================================
# 환경 변수
# ============================================================================

WORKFLOW_AUTOMATION_API_KEY = os.environ.get("WORKFLOW_AUTOMATION_API_KEY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
PLAN_MD_REPO = "team-monolith-product/plan-md"

if not WORKFLOW_AUTOMATION_API_KEY:
    raise RuntimeError("WORKFLOW_AUTOMATION_API_KEY 환경 변수가 설정되지 않았습니다.")

if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN 환경 변수가 설정되지 않았습니다.")


# ============================================================================
# FastAPI 앱 초기화
# ============================================================================

app = FastAPI(
    title="Workflow Automation API",
    description="노션 Webhook 및 자동화 작업을 처리하는 API",
    version="1.0.0",
)


# ============================================================================
# Request/Response 모델
# ============================================================================


class NotionAutomationSource(BaseModel):
    """Notion Automation 소스 정보"""

    type: str  # "automation"
    automation_id: str
    action_id: str
    event_id: str
    user_id: str
    attempt: int


class NotionPageProperty(BaseModel):
    """Notion 페이지 속성 (동적으로 처리)"""

    pass


class WebhookPayload(BaseModel):
    """노션 Automation Webhook 페이로드"""

    source: NotionAutomationSource
    data: dict  # Notion page object (dict[str, Any] causes runtime error in Python 3.9)


class WebhookResponse(BaseModel):
    """Webhook 처리 결과 응답"""

    status: str
    message: str
    pr_url: Optional[str] = None
    task_id: Optional[str] = None


# ============================================================================
# 헬퍼 함수
# ============================================================================


def extract_task_id(properties: dict) -> Optional[str]:
    """Notion 페이지 속성에서 TASK ID 추출"""
    id_prop = properties.get("ID", {})
    if id_prop.get("type") == "unique_id":
        unique_id = id_prop.get("unique_id", {})
        prefix = unique_id.get("prefix", "TASK")
        number = unique_id.get("number")
        if number:
            return f"{prefix}-{number}"
    return None


def extract_title(properties: dict) -> str:
    """Notion 페이지 속성에서 제목 추출"""
    title_prop = properties.get("제목", {})
    if title_prop.get("type") == "title":
        title_items = title_prop.get("title", [])
        if title_items:
            return title_items[0].get("plain_text", "Untitled")
    return "Untitled"


def get_notion_markdown(page_id: str) -> str:
    """Notion 페이지를 마크다운으로 변환"""
    return notion_page_to_markdown(page_id)


def create_branch_name(task_id: str) -> str:
    """브랜치 이름 생성: TASK-{ID}-YYMMDDHHMM"""
    now = datetime.now()
    timestamp = now.strftime("%y%m%d%H%M")
    return f"{task_id}-{timestamp}"


def sanitize_filename(filename: str) -> str:
    """파일명에서 사용 불가능한 문자 제거"""
    return filename.replace("/", "-").replace("\\", "-").replace(":", "-")


def find_existing_file(repo, task_id: str) -> Optional[str]:
    """
    TASK-{ID}로 시작하는 파일 검색

    Returns:
        기존 파일 경로 또는 None
    """
    try:
        contents = repo.get_contents("", ref="main")
        for content in contents:
            if content.type == "file" and content.name.startswith(f"[{task_id}]"):
                return content.path
    except GithubException:
        pass
    return None


def create_or_update_file_via_api(
    repo,
    file_path: str,
    content: str,
    task_id: str,
    title: str,
    branch_name: str,
    existing_file: Optional[str] = None,
) -> None:
    """
    GitHub API를 통해 파일 생성 또는 업데이트

    Args:
        repo: GitHub repository object
        file_path: 새 파일 경로
        content: 파일 내용
        task_id: TASK ID
        title: 문서 제목
        branch_name: 브랜치 이름
        existing_file: 기존 파일 경로 (있는 경우)
    """
    action = "Update" if existing_file else "Create"
    commit_message = f"{action} [{task_id}] {title}"

    # main 브랜치 참조 가져오기
    main_ref = repo.get_git_ref("heads/main")
    main_sha = main_ref.object.sha

    # 새 브랜치 생성
    repo.create_git_ref(f"refs/heads/{branch_name}", main_sha)

    # 파일명이 변경된 경우 (제목이 바뀐 경우)
    if existing_file and existing_file != file_path:
        # 기존 파일 삭제
        old_file = repo.get_contents(existing_file, ref="main")
        repo.delete_file(
            existing_file,
            f"Remove old file for [{task_id}]",
            old_file.sha,
            branch=branch_name,
        )
        # 새 파일 생성
        repo.create_file(file_path, commit_message, content, branch=branch_name)
    elif existing_file:
        # 기존 파일 업데이트
        file_content = repo.get_contents(file_path, ref="main")
        repo.update_file(
            file_path, commit_message, content, file_content.sha, branch=branch_name
        )
    else:
        # 새 파일 생성
        repo.create_file(file_path, commit_message, content, branch=branch_name)


def create_pull_request(repo, task_id: str, title: str, branch_name: str) -> str:
    """GitHub PR 생성"""
    now = datetime.now()
    timestamp = now.strftime("%y%m%d %H:%M")

    pr_title = f"[{task_id}] {title} {timestamp} 변동 안내"
    pr_body = f"""## 변경 내용
- TASK ID: {task_id}
- 문서 제목: {title}
- 업데이트 시각: {timestamp}

이 PR은 Notion Automation에 의해 자동으로 생성되었습니다.
"""

    pr = repo.create_pull(
        title=pr_title,
        body=pr_body,
        head=branch_name,
        base="main",
    )

    return pr.html_url


# ============================================================================
# 인증 의존성
# ============================================================================


async def verify_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    """
    API Key 헤더 검증

    요청 헤더에 'X-API-Key: <WORKFLOW_AUTOMATION_API_KEY>' 가 있어야 합니다.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key 헤더가 필요합니다",
        )

    if x_api_key != WORKFLOW_AUTOMATION_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="유효하지 않은 API Key입니다",
        )


# ============================================================================
# 엔드포인트
# ============================================================================


@app.get("/health")
async def health_check():
    """
    헬스 체크 엔드포인트

    Kubernetes liveness/readiness probe에서 사용됩니다.
    """
    return {
        "status": "healthy",
        "service": "workflow-automation-api",
    }


@app.post("/webhook", response_model=WebhookResponse)
async def handle_webhook(
    payload: WebhookPayload,
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    노션 Automation Webhook 처리 엔드포인트

    노션 버튼 클릭 시 plan-md 레포에 PR을 생성합니다:
    1. plan-md 레포 클론
    2. TASK-{ID}-YYMMDDHHMM 브랜치 생성
    3. Notion 페이지 → 마크다운 변환
    4. [TASK-{ID}] {제목}.md 파일 생성/업데이트
    5. PR 생성

    Headers:
        X-API-Key: 인증용 API Key (필수)

    Request Body:
        source: Notion automation 정보
        data: Notion page object

    Returns:
        WebhookResponse: PR URL 포함 처리 결과
    """
    start_time = time.time()
    logger.info("=" * 80)
    logger.info("웹훅 처리 시작")

    # API Key 검증
    step_start = time.time()
    await verify_api_key(x_api_key)
    logger.info(f"[1/7] API Key 검증 완료 (소요시간: {time.time() - step_start:.3f}초)")

    try:
        # 무거운 동기 작업들을 별도 스레드에서 실행하여 이벤트 루프 블로킹 방지

        # GitHub 클라이언트 초기화
        step_start = time.time()
        gh = Github(GITHUB_TOKEN)
        repo = gh.get_repo(PLAN_MD_REPO)
        logger.info(
            f"[2/7] GitHub 클라이언트 초기화 완료 (소요시간: {time.time() - step_start:.3f}초)"
        )

        # Notion 페이지 정보 추출
        step_start = time.time()
        page_data = payload.data
        page_id = page_data.get("id", "").replace("-", "")  # ID에서 하이픈 제거
        properties = page_data.get("properties", {})

        # TASK ID 및 제목 추출
        task_id = extract_task_id(properties)
        if not task_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="TASK ID를 찾을 수 없습니다",
            )

        title = extract_title(properties)
        logger.info(
            f"[3/7] 페이지 정보 추출 완료 - Task ID: {task_id}, Title: {title} (소요시간: {time.time() - step_start:.3f}초)"
        )

        # 브랜치 이름 생성
        step_start = time.time()
        branch_name = create_branch_name(task_id)
        logger.info(
            f"[4/7] 브랜치 이름 생성 완료: {branch_name} (소요시간: {time.time() - step_start:.3f}초)"
        )

        # Notion → 마크다운 변환 (무거운 작업 - 별도 스레드에서 실행)
        step_start = time.time()
        markdown_content = await asyncio.to_thread(get_notion_markdown, page_id)
        logger.info(
            f"[5/7] Notion → 마크다운 변환 완료 (소요시간: {time.time() - step_start:.3f}초)"
        )

        # 기존 파일 검색 (별도 스레드에서 실행)
        step_start = time.time()
        existing_file = await asyncio.to_thread(find_existing_file, repo, task_id)
        logger.info(
            f"[6/7] 기존 파일 검색 완료 - 기존 파일: {existing_file or '없음'} (소요시간: {time.time() - step_start:.3f}초)"
        )

        # 파일 경로 결정
        filename = f"[{task_id}] {title}.md"
        filename = sanitize_filename(filename)

        # GitHub API를 통해 파일 생성/업데이트 (무거운 작업 - 별도 스레드에서 실행)
        step_start = time.time()
        await asyncio.to_thread(
            create_or_update_file_via_api,
            repo,
            filename,
            markdown_content,
            task_id,
            title,
            branch_name,
            existing_file,
        )
        action = "업데이트" if existing_file else "생성"
        logger.info(
            f"[7/7] GitHub 파일 {action} 완료: {filename} (소요시간: {time.time() - step_start:.3f}초)"
        )

        # PR 생성 (별도 스레드에서 실행)
        step_start = time.time()
        pr_url = await asyncio.to_thread(
            create_pull_request, repo, task_id, title, branch_name
        )
        logger.info(
            f"PR 생성 완료: {pr_url} (소요시간: {time.time() - step_start:.3f}초)"
        )

        total_time = time.time() - start_time
        logger.info(f"웹훅 처리 완료 ✓ (총 소요시간: {total_time:.3f}초)")
        logger.info("=" * 80)

        return WebhookResponse(
            status="success",
            message=f"PR이 성공적으로 생성되었습니다",
            pr_url=pr_url,
            task_id=task_id,
        )

    except GithubException as e:
        total_time = time.time() - start_time
        logger.error(f"GitHub API 오류 발생 (총 소요시간: {total_time:.3f}초)")
        logger.error(f"오류 상세: {e}")
        logger.info("=" * 80)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"GitHub 작업 중 오류가 발생했습니다: {e.data.get('message', str(e))}",
        )
    except Exception as e:
        total_time = time.time() - start_time
        logger.error(f"Webhook 처리 실패 (총 소요시간: {total_time:.3f}초)")
        logger.error(f"오류 상세: {e}", exc_info=True)
        logger.info("=" * 80)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Webhook 처리 중 오류가 발생했습니다: {str(e)}",
        )


@app.get("/")
async def root():
    """루트 엔드포인트 - API 정보 반환"""
    return {
        "service": "Workflow Automation API",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "webhook": "/webhook",
            "docs": "/docs",
        },
    }


# ============================================================================
# 예외 핸들러
# ============================================================================


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTP 예외 핸들러"""
    logger.warning(f"HTTP Exception: {exc.status_code} - {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "message": exc.detail,
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """일반 예외 핸들러"""
    # 프로덕션 환경에서는 상세 에러를 숨기고 로깅만 수행
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "message": "Internal server error",
        },
    )


# ============================================================================
# 서버 실행 (개발용)
# ============================================================================

if __name__ == "__main__":
    # 로컬 개발 시 직접 실행
    # 프로덕션에서는 uvicorn CLI 사용: uvicorn main:app --host 0.0.0.0 --port 8000
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
