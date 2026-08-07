"""
모든 LangChain 도구가 async인지 검사하는 테스트

동기 `@tool def`가 하나라도 있으면 langchain이 `invoke` 전체를 워커 스레드로 넘기고
그 안에서 콜백을 새 이벤트 루프로 돌린다. 그러면 한 요청의 콜백이 여러 루프로 갈라져
콜백 핸들러가 들고 있는 `asyncio.Lock` 같은 루프 종속 객체가 깨진다.

도구는 팩토리 안에서 만들어지는 것도 있어 런타임 수집으로는 전수 확인이 안 된다.
소스를 직접 훑어 `@tool` 데코레이터가 붙은 정의를 전부 찾는다.
"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SEARCH_DIRS = ("api", "app", "scripts", "service")


def _is_tool_decorator(decorator: ast.expr) -> bool:
    """`@tool` 또는 `@tool(...)` 데코레이터인지 판별한다"""
    if isinstance(decorator, ast.Call):
        decorator = decorator.func
    return isinstance(decorator, ast.Name) and decorator.id == "tool"


def _find_tools(path: Path) -> list[tuple[str, bool]]:
    """파일에서 도구 정의를 찾아 (`파일:줄 이름`, async 여부)로 모은다"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    relative = path.relative_to(PROJECT_ROOT)

    return [
        (
            f"{relative}:{node.lineno} {node.name}",
            isinstance(node, ast.AsyncFunctionDef),
        )
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_tool_decorator(d) for d in node.decorator_list)
    ]


def test_all_tools_are_async():
    """`@tool`이 붙은 정의는 전부 `async def`여야 한다"""
    tools = [
        entry
        for directory in SEARCH_DIRS
        for path in sorted((PROJECT_ROOT / directory).rglob("*.py"))
        for entry in _find_tools(path)
    ]

    # 데코레이터 이름이 바뀌어 스캐너가 헛도는 상태를 막는다
    assert len(tools) > 10, f"도구를 {len(tools)}개만 찾았다. 스캐너를 확인할 것"

    sync_tools = [name for name, is_async in tools if not is_async]
    assert not sync_tools, (
        "동기 도구가 있으면 콜백이 별도 이벤트 루프에서 실행된다. "
        "`async def`로 바꾸고 블로킹 호출은 `asyncio.to_thread`로 넘길 것:\n"
        + "\n".join(sync_tools)
    )
