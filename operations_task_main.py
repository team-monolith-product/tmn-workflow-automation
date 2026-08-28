"""운영팀 Slack 작업 MCP의 독립 배포 진입점입니다."""

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from app.slack_task_mcp import build_mcp, build_mcp_app

load_dotenv()

operations_task_mcp = build_mcp()
operations_task_mcp_app = build_mcp_app(operations_task_mcp)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """MCP 세션 매니저를 운영 서비스 수명에 맞춰 실행합니다."""
    async with operations_task_mcp.session_manager.run():
        yield


app = FastAPI(
    title="Operations Slack Task MCP",
    description="운영팀 Slack List 작업 전용 MCP",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    """배포 환경의 liveness/readiness probe입니다."""
    return {"status": "ok"}


app.mount("/", operations_task_mcp_app)
