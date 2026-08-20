"""
발송 계층입니다. 보내기 전에 걸릴 것을 모아 보여주고, 벤더를 한 번 부릅니다.

캠페인 전체가 요청 1회입니다. 문안 하나에 targets 배열을 실으면 이름·기수는
벤더가 치환합니다.
"""

from typing import Any, NamedTuple

from service.sms import templates, transport


class Plan(NamedTuple):
    """보내기 전에 정해지는 것 전부. problems 가 비어야 보낼 수 있습니다."""

    problems: list[str]
    template: str
    rows: list[dict[str, Any]]  # 번호 정규화 + 중복 접기까지 끝난 목록
    folded: int  # 접힌 중복 번호 수
    message_type: str | None


def _plan(rows: list[dict[str, Any]], content: str | None) -> Plan:
    """보낼 수 있는지 판정하면서, 보낼 때 쓸 값을 같이 만듭니다.

    판정과 산출을 갈라 두면 사본이 생겨, 미리보기와 실제 발송이 다른 말을 합니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        content: 문안 본문

    Returns:
        Plan: 판정 결과와 발송에 쓸 값
    """
    if not content:
        return Plan(["문안이 비어 있습니다."], "", [], 0, None)
    template = content.rstrip("\n")

    if not rows:
        return Plan(["수신자가 없습니다."], template, [], 0, None)

    problems: list[str] = []
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            # 모델이 만든 목록이라 to 가 없거나 수로 올 수 있다. 그것도
            # "형식 오류" 로 모아 돌려줘야 KeyError 로 새지 않는다.
            phone = templates.normalize_phone(str(row.get("to", "")))
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

    return Plan(problems, template, folded, len(rows) - len(folded), message_type)


def preview(rows: list[dict[str, Any]], content: str | None = None) -> dict[str, Any]:
    """보내지 않고 걸릴 것과 나갈 모양을 봅니다.

    Args:
        rows: 수신자 목록
        content: 문안 본문

    Returns:
        dict[str, Any]: problems·message_type·max_bytes·targets·folded·sample.
            problems 가 비어 있어야 보낼 수 있습니다
    """
    plan = _plan(rows, content)
    rendered = [templates.render(plan.template, row) for row in plan.rows]
    return {
        "problems": plan.problems,
        "message_type": plan.message_type,
        "max_bytes": max(
            (templates.euckr_len(text) for text in [plan.template, *rendered]),
            default=0,
        ),
        "targets": len(plan.rows),
        "folded": plan.folded,
        "sample": rendered[0] if rendered else "",
    }


def send(
    *, rows: list[dict[str, Any]], content: str, subject: str | None = None
) -> dict[str, Any]:
    """문자를 보냅니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        content: 문안 본문
        subject: LMS 제목. 생략하면 '안내'

    Returns:
        dict[str, Any]: sent·message_key·message_type

    Raises:
        ValueError: preview 가 걸러내는 것에 걸렸을 때
        transport.PpurioError: 벤더가 거절했을 때
    """
    plan = _plan(rows, content)
    if plan.problems:
        raise ValueError(" / ".join(plan.problems))

    payload = {
        "messageType": plan.message_type,
        "content": plan.template,
        "targetCount": len(plan.rows),
        "targets": templates.build_targets(plan.rows),
    }
    if plan.message_type != "SMS":
        payload["subject"] = subject or "안내"

    result = transport.send(payload)
    if str(result.get("code")) != "1000":
        raise transport.PpurioError(200, result)

    return {
        "sent": len(plan.rows),
        "message_key": result.get("messageKey"),
        "message_type": plan.message_type,
    }
