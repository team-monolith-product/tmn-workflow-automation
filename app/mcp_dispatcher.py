"""공용 FastAPI 서버에서 서로 다른 MCP 경로를 분기합니다."""

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

OAUTH_METADATA_PATH = "/.well-known/oauth-protected-resource"
OPERATIONS_MCP_PATH = "/mcp/operate"


class MCPPathDispatcher:
    """MCP 앱의 자체 미들웨어를 유지한 채 공개 경로만 나눕니다."""

    def __init__(self, knowledge_app: ASGIApp, operations_app: ASGIApp):
        self.knowledge_app = knowledge_app
        self.operations_app = operations_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1000})
            return

        path = scope["path"]
        if path == OPERATIONS_MCP_PATH or path.startswith(f"{OPERATIONS_MCP_PATH}/"):
            await self.operations_app(scope, receive, send)
            return

        if path == "/mcp" or path.startswith("/mcp/") or path == OAUTH_METADATA_PATH:
            await self.knowledge_app(scope, receive, send)
            return

        await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
