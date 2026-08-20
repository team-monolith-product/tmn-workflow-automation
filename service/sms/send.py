"""
발송 계층입니다. 보내기 전에 걸릴 것을 모아 보여주고, 벤더를 한 번 부릅니다.

캠페인 전체가 요청 1회입니다. 문안 하나에 targets 배열을 실으면 이름·기수는
벤더가 치환합니다.
"""

import datetime
from typing import Any, NamedTuple

from service.sms import KST, templates, transport

# 뿌리오는 이보다 가까운 예약을 거부합니다.
MIN_RESERVE_SECONDS = 180


class Plan(NamedTuple):
    """보내기 전에 정해지는 것 전부. problems 가 비어야 보낼 수 있습니다."""

    problems: list[str]
    template: str
    rows: list[dict[str, Any]]  # 번호 정규화 + 중복 접기까지 끝난 목록
    folded: int
    message_type: str | None
    send_time: str | None  # 벤더 sendTime. 즉시 발송이면 None


def _plan(
    rows: list[dict[str, Any]],
    content: str | None,
    send_at: datetime.datetime | None,
) -> Plan:
    """보낼 수 있는지 판정하면서, 보낼 때 쓸 값을 같이 만듭니다.

    판정과 산출을 갈라 두면 사본이 생겨, check 와 실제 발송이 다른 말을 합니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        content: 문안 본문
        send_at: 예약 발송 시각. 없으면 즉시 발송

    Returns:
        Plan: 판정 결과와 발송에 쓸 값
    """
    if not content:
        return Plan(["문안이 비어 있습니다."], "", [], 0, None, None)
    template = content.rstrip("\n")

    if not rows:
        return Plan(["수신자가 없습니다."], template, [], 0, None, None)

    problems: list[str] = []
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            phone = templates.normalize_phone(row["to"])
        except ValueError as error:
            problems.append(str(error))
            continue
        unique.setdefault(phone, {**row, "to": phone})
    folded = list(unique.values())

    message_type = None
    if folded:
        try:
            message_type = templates.decide_message_type(template, folded)
        except ValueError as error:
            problems.append(str(error))

    send_time = None
    if send_at is not None:
        try:
            send_time = reserve_time(send_at)
        except ValueError as error:
            problems.append(str(error))

    return Plan(
        problems, template, folded, len(rows) - len(folded), message_type, send_time
    )


def send(
    *,
    rows: list[dict[str, Any]],
    content: str,
    subject: str | None = None,
    send_at: datetime.datetime | None = None,
    **vendor: Any,
) -> dict[str, Any]:
    """문자를 보냅니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        content: 문안 본문
        subject: LMS 제목. 생략하면 '안내'
        send_at: 예약 발송 시각. 생략하면 즉시. 최소 3분 뒤여야 한다
        **vendor: 뿌리오 payload 에 그대로 실을 추가 필드

    Returns:
        dict[str, Any]: sent·sent_to·message_key·message_type

    Raises:
        ValueError: check 가 걸러내는 것에 걸렸을 때
        transport.PpurioError: 벤더가 거절했을 때
    """
    plan = _plan(rows, content, send_at)
    if plan.problems:
        raise ValueError(" / ".join(plan.problems))
    if plan.send_time is not None:
        vendor["sendTime"] = plan.send_time

    payload = {
        "messageType": plan.message_type,
        "content": plan.template,
        "targetCount": len(plan.rows),
        "targets": templates.build_targets(plan.rows),
        **vendor,
    }
    if plan.message_type != "SMS":
        payload["subject"] = subject or "안내"

    result = transport.send(payload)
    if str(result.get("code")) != "1000":
        raise transport.PpurioError(200, result)

    return {
        "sent": len(plan.rows),
        "sent_to": [row["to"] for row in plan.rows],
        "message_key": result.get("messageKey"),
        "message_type": plan.message_type,
    }


def reserve_time(send_at: datetime.datetime) -> str:
    """예약 시각을 검사해 벤더 형식으로 바꿉니다.

    Args:
        send_at: 예약 발송 시각

    Returns:
        str: yyyy-MM-ddTHH:mm:ss

    Raises:
        ValueError: 지금부터 MIN_RESERVE_SECONDS 보다 가까울 때
    """
    # 컨테이너의 now() 는 UTC 라 그대로 빼면 이미 지난 시각도 통과한다.
    if send_at.tzinfo is not None:
        send_at = send_at.astimezone(KST).replace(tzinfo=None)
    now = datetime.datetime.now(KST).replace(tzinfo=None)
    margin = (send_at - now).total_seconds()
    if margin < MIN_RESERVE_SECONDS:
        raise ValueError(
            f"예약은 최소 {MIN_RESERVE_SECONDS // 60}분 뒤여야 합니다 "
            f"(지금 {margin / 60:.1f}분 뒤로 지정됨)"
        )
    return send_at.strftime("%Y-%m-%dT%H:%M:%S")


def check(
    rows: list[dict[str, Any]],
    content: str | None = None,
    send_at: datetime.datetime | None = None,
) -> list[str]:
    """보내기 전에 걸릴 것들을 전부 모읍니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        content: 문안 본문
        send_at: 예약 발송 시각

    Returns:
        list[str]: 보낼 수 없는 이유. 비어 있으면 보낼 수 있다
    """
    return _plan(rows, content, send_at).problems


def cancel_reserved(message_key: str) -> dict[str, Any]:
    """예약 발송을 취소합니다. 발송 1분 전까지만 가능합니다.

    Args:
        message_key: 접수 시 받은 messageKey

    Returns:
        dict[str, Any]: 벤더 응답

    Raises:
        transport.PpurioError: 취소가 접수되지 않았을 때
    """
    result = transport.cancel(message_key)
    if str(result.get("code")) != "1000":
        raise transport.PpurioError(200, result)
    return result


def preview(rows: list[dict[str, Any]], content: str | None = None) -> dict[str, Any]:
    """발송하지 않고 문안·타입·길이만 확인합니다.

    Args:
        rows: 수신자 목록
        content: 문안 본문

    Returns:
        dict[str, Any]: message_type·max_bytes·targets·folded·sample
    """
    plan = _plan(rows, content, None)
    rendered = [templates.render(plan.template, row) for row in plan.rows]
    return {
        "message_type": plan.message_type,
        "max_bytes": max((templates.euckr_len(text) for text in rendered), default=0),
        "targets": len(plan.rows),
        "folded": plan.folded,
        "sample": rendered[0] if rendered else "",
    }
