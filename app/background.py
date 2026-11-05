"""
기다리지 않고 띄워 두는 백그라운드 작업입니다.

asyncio.create_task 는 태스크를 약하게만 붙잡아서, 참조를 들고 있지 않으면 도는
도중에 GC 될 수 있습니다. 띄우는 곳마다 집합을 따로 두지 않고 여기 하나에 모읍니다.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

# 도는 중인 태스크. 끝나면 스스로 빠진다.
_running: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """코루틴을 백그라운드 태스크로 띄우고, 끝날 때까지 참조를 붙잡아 둡니다.

    Args:
        coro: 띄울 코루틴

    Returns:
        asyncio.Task: 띄운 태스크. 기다릴 필요가 있을 때만 씁니다(테스트 등).
    """
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task
