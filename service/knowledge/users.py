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
