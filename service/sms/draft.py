"""
승인 대기 중인 발송 초안입니다.

슬랙 버튼의 value 는 2000자 제한이 있어 수신자 목록을 통째로 실을 수 없습니다.
초안을 여기 두고 버튼에는 id 만 싣습니다.

프로세스 메모리에 둡니다. 초안은 몇 분 안에 승인되거나 버려지고, 봇이 재시작되면
사라져도 사람이 다시 요청하면 그만입니다. 승인 버튼이 죽었다는 것은 사람에게
분명히 보이므로 조용한 실패가 아닙니다.
"""

import datetime
import uuid
from typing import Any, NamedTuple

from service.sms import KST

# 초안이 살아 있는 시간. 이보다 오래된 것은 승인해도 거절합니다 — 어제 올린
# 카드를 오늘 눌러 엉뚱한 문자가 나가는 것을 막습니다.
LIFETIME = datetime.timedelta(hours=6)


class Draft(NamedTuple):
    """승인을 기다리는 발송 한 건."""

    id: str
    campaign: str | None
    content: str
    rows: list[dict[str, Any]]
    requested_by: str  # 슬랙 사용자 ID. 이 사람만 승인할 수 있다
    channel_id: str
    created_at: datetime.datetime


_DRAFTS: dict[str, Draft] = {}


def put(
    *,
    campaign: str | None,
    content: str,
    rows: list[dict[str, Any]],
    requested_by: str,
    channel_id: str,
) -> Draft:
    """초안을 보관하고 돌려줍니다.

    Args:
        campaign: 발송 건 식별자. None 이면 개인 CS
        content: 치환 전 원문
        rows: to·name·var1~var8 을 담은 수신자 목록
        requested_by: 요청한 슬랙 사용자 ID
        channel_id: 요청이 온 채널

    Returns:
        Draft: 보관된 초안
    """
    draft = Draft(
        id=uuid.uuid4().hex[:12],
        campaign=campaign,
        content=content,
        rows=rows,
        requested_by=requested_by,
        channel_id=channel_id,
        created_at=datetime.datetime.now(KST),
    )
    _DRAFTS[draft.id] = draft
    return draft


def take(draft_id: str) -> Draft | None:
    """초안을 꺼내고 지웁니다. 없거나 만료됐으면 None.

    꺼내면서 지우는 이유는 두 번 눌렀을 때 두 번 보내지 않기 위해서입니다.
    실제 중복 차단은 DB 가 하지만, 여기서 먼저 막으면 벤더를 두 번 부르지
    않습니다.

    Args:
        draft_id: 버튼에 실린 초안 id

    Returns:
        Draft | None: 살아 있는 초안
    """
    draft = _DRAFTS.pop(draft_id, None)
    if draft is None:
        return None
    if datetime.datetime.now(KST) - draft.created_at > LIFETIME:
        return None
    return draft


def restore(item: Draft) -> None:
    """꺼낸 초안을 같은 id 로 되돌립니다.

    승인 권한이 없는 사람이 눌렀을 때 씁니다. 새 id 로 다시 넣으면 카드에
    박힌 버튼이 죽은 id 를 가리켜 요청자도 못 누르게 됩니다.

    Args:
        item: take 로 꺼낸 초안
    """
    _DRAFTS[item.id] = item


def drop(draft_id: str) -> Draft | None:
    """초안을 취소합니다.

    Args:
        draft_id: 버튼에 실린 초안 id

    Returns:
        Draft | None: 버려진 초안
    """
    return _DRAFTS.pop(draft_id, None)
