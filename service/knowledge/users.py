"""
Slack UID를 이메일로 바꾸는 매핑을 제공합니다.

이메일을 식별자로 쓰는 이유는 노션·드라이브·GitHub에서 같은 사람을 가리키기
때문입니다. Slack UID는 Slack 안에서만 통합니다.

메시지마다 딸려오는 user_profile을 쓰지 않고 워크스페이스 단위로 한 번
받아옵니다. 사람이 100명 남짓인데 Export 1년치에서 33만 번 복사되어 42MB를
차지했습니다.
"""

import json
import pathlib
from typing import Any

from cachetools import TTLCache
from slack_sdk.web.async_client import AsyncWebClient

# 입퇴사 반영은 하루면 충분하다.
_cache: TTLCache = TTLCache(maxsize=1, ttl=86400)


def _to_email_map(members: list[dict[str, Any]]) -> dict[str, str]:
    """users.list 응답에서 UID → 이메일 매핑을 만듭니다.

    Args:
        members: Slack 사용자 객체 목록

    Returns:
        dict[str, str]: 이메일이 있는 계정만 담은 매핑
    """
    return {
        member["id"]: (member.get("profile") or {})["email"]
        for member in members
        if (member.get("profile") or {}).get("email")
    }


def load_from_export(export_root: pathlib.Path) -> dict[str, str]:
    """Export의 users.json에서 매핑을 읽습니다.

    Args:
        export_root: Export 압축을 푼 디렉터리

    Returns:
        dict[str, str]: UID → 이메일
    """
    members = json.loads((export_root / "users.json").read_text(encoding="utf-8"))
    return _to_email_map(members)


async def fetch_user_emails(client: AsyncWebClient) -> dict[str, str]:
    """워크스페이스 사용자 매핑을 받아옵니다. 하루 동안 캐시합니다.

    Args:
        client: Slack 클라이언트

    Returns:
        dict[str, str]: UID → 이메일
    """
    if "map" in _cache:
        return _cache["map"]

    members: list[dict[str, Any]] = []
    cursor = None
    while True:
        response = await client.users_list(limit=200, cursor=cursor)
        members.extend(response["members"])
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    emails = _to_email_map(members)
    _cache["map"] = emails
    return emails
