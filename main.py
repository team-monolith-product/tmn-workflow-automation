"""
Workflow Automation FastAPI Server

노션 버튼 클릭 시 Webhook을 받아 자동화 작업을 수행하는 경량 FastAPI 서버입니다.
기존 Slack Bot(app.py)과 동일한 컨테이너에서 실행되며, 공유 모듈을 활용합니다.
"""

import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

# 기존 모듈 임포트
# from app.common import ...  # 필요시 공유 함수 사용
# from api import ...  # 필요시 API 클라이언트 사용
# from service import ...  # 필요시 서비스 모듈 사용


# ============================================================================
# 환경 변수
# ============================================================================

WORKFLOW_AUTOMATION_API_KEY = os.environ.get("WORKFLOW_AUTOMATION_API_KEY")
if not WORKFLOW_AUTOMATION_API_KEY:
    raise RuntimeError(
        "WORKFLOW_AUTOMATION_API_KEY 환경 변수가 설정되지 않았습니다. "
        "이 변수는 API 인증에 필요합니다."
    )


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


class WebhookPayload(BaseModel):
    """
    노션 버튼 클릭 시 전송되는 Webhook 페이로드

    실제 노션에서 전송하는 데이터 형식에 맞게 수정하세요.
    """

    action: str  # 예: "create_task", "update_status" 등
    notion_page_id: Optional[str] = None
    data: Optional[dict] = None


class WebhookResponse(BaseModel):
    """Webhook 처리 결과 응답"""

    status: str
    message: str
    data: Optional[dict] = None


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
    _: None = Header(None, alias="X-API-Key", include_in_schema=False),
):
    """
    노션 Webhook 처리 엔드포인트
    
    노션 버튼 클릭 시 이 엔드포인트로 POST 요청이 전송됩니다.
    
    Headers:
        X-API-Key: 인증용 API Key (필수)
    
    Request Body:
        action: 수행할 작업 유형
        notion_page_id: 노션 페이지 ID (선택)
        data: 추가 데이터 (선택)
    
    Returns:
        WebhookResponse: 처리 결과
    
    Example:
        ```bash
        curl -X POST https://wfa.codle.io/webhook \\
          -H "Content-Type: application/json" \\
          -H "X-API-Key: your-api-key" \\
          -d '{"action": "create_task", "notion_page_id": "abc123", "data": {}}'
        ```
    """
    # API Key 검증
    await verify_api_key(request.headers.get("x-api-key"))

    # TODO: 실제 자동화 로직 구현
    # 예시:
    # if payload.action == "create_task":
    #     # Notion API로 작업 생성
    #     # GitHub API로 이슈 생성
    #     pass
    # elif payload.action == "update_status":
    #     # 상태 업데이트 로직
    #     pass

    return WebhookResponse(
        status="success",
        message=f"Webhook received for action: {payload.action}",
        data={
            "received_action": payload.action,
            "notion_page_id": payload.notion_page_id,
        },
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
    print(f"[ERROR] Unhandled exception: {exc}")
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
