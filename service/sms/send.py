"""
발송 계층입니다. 보내기 전에 걸릴 것을 모아 보여주고, 벤더를 한 번 부릅니다.

캠페인 전체가 요청 1회입니다. 문안 하나에 targets 배열을 실으면 이름·기수는
벤더가 치환합니다.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from service.sms import templates, transport

# 벤더는 예약 시각을 한국 시간으로 읽습니다. 서버가 UTC 로 도는 곳에 올라가면
# naive datetime 은 9시간 어긋나 "지금 보내라" 가 아침으로 밀립니다.
KST = timezone(timedelta(hours=9))
# 벤더가 요구하는 최소 여유. 이보다 촉박하면 접수 자체를 거절합니다.
MIN_LEAD = timedelta(minutes=3)
SEND_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


class Plan(NamedTuple):
    """보내기 전에 정해지는 것 전부. problems 가 비어야 보낼 수 있습니다."""

    problems: list[str]
    template: str
    rows: list[dict[str, Any]]  # 번호 정규화 + 중복 접기까지 끝난 목록
    folded: int  # 접힌 중복 번호 수
    message_type: str | None
    sample: str
    max_bytes: int
    targets: list[dict[str, Any]]  # 벤더로 그대로 나가는 값. 카드도 이걸 보여준다
    send_at: datetime | None = None  # 예약 시각(aware). None 이면 즉시 발송


class Sent(NamedTuple):
    """벤더로 실제 나간 값.

    이력이 남기는 것과 카드가 그리는 것이 같은 출처여야 합니다. 이름 없는
    dict 로 돌려주면 어느 쪽이 키를 빠뜨려도 조용히 지나가고, 그걸 막으려고
    테스트가 키마다 사슬을 손으로 잠그게 됩니다.
    """

    sent: int  # 접힌 중복을 뺀 실제 수신자 수
    ref_key: str  # 한 번의 발송을 묶는 우리 쪽 키
    sender: str
    message_key: str | None  # 뿌리오 접수 키. 벤더가 빠뜨릴 수 있다
    message_type: str
    send_at: datetime | None
    content: str  # 치환 전 원문
    targets: list[dict[str, Any]]


def parse_send_at(value: str) -> tuple[datetime | None, str | None]:
    """예약 시각 문자열을 읽습니다.

    벤더 형식 문자열이 아니라 datetime 을 돌려줍니다. 문자열로 눌러 두면
    쓰는 쪽마다 다시 파싱하면서 시간대를 잃고, 그걸 손으로 도로 붙이게 됩니다.

    Args:
        value: "2026-08-22 09:00" 같은 한국 시간. 빈 값이면 즉시 발송

    Returns:
        tuple: (예약 시각, 문제). 둘 중 하나만 채워집니다
    """
    text = (value or "").strip()
    if not text:
        return None, None
    try:
        when = datetime.fromisoformat(text.replace("/", "-"))
    except ValueError:
        return None, f"예약 시각을 읽을 수 없습니다: {value}"
    # 시간대가 없으면 한국 시간으로 읽습니다. 사람이 말한 시각은 늘 한국 시간입니다.
    if when.tzinfo is None:
        when = when.replace(tzinfo=KST)
    lead = when - datetime.now(tz=KST)
    if lead < MIN_LEAD:
        return None, (
            f"예약은 지금부터 {int(MIN_LEAD.total_seconds() // 60)}분 뒤부터 됩니다"
            f" (지정하신 시각은 {round(lead.total_seconds() / 60)}분 뒤)"
        )
    return when.astimezone(KST), None


def preview(
    rows: list[dict[str, Any]], content: str | None = None, send_at: str = ""
) -> Plan:
    """보내지 않고 걸릴 것과 나갈 모양을 봅니다.

    판정과 산출이 같은 함수입니다. 갈라 두면 사본이 생겨, 미리보기와 실제
    발송이 다른 말을 합니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        content: 문안 본문
        send_at: 예약 시각(한국 시간). 빈 값이면 즉시 발송

    Returns:
        Plan: problems 가 비어 있어야 보낼 수 있습니다
    """
    template = (content or "").strip()
    if not template:
        return Plan(["문안이 비어 있습니다."], "", [], 0, None, "", 0, [])
    if not rows:
        return Plan(["수신자가 없습니다."], template, [], 0, None, "", 0, [])

    problems: list[str] = []
    send_at_value, when_problem = parse_send_at(send_at)
    if when_problem:
        problems.append(when_problem)

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
    kept = list(unique.values())

    rendered = [templates.render(template, row) for row in kept]
    # 벤더로 나가는 것은 치환 전 원문이라 벤더도 원문 길이로 타입을 본다.
    # 치환 후만 재면 SMS 로 판정해 놓고 90byte 넘는 원문을 보내게 된다.
    max_bytes = max(templates.euckr_len(text) for text in [template, *rendered])
    if max_bytes > templates.LMS_MAX_BYTES:
        problems.append(
            f"치환 후 {max_bytes}byte — LMS 한도 {templates.LMS_MAX_BYTES} 초과"
        )

    return Plan(
        problems,
        template,
        kept,
        len(rows) - len(kept),
        "SMS" if max_bytes <= templates.SMS_MAX_BYTES else "LMS",
        rendered[0] if rendered else "",
        max_bytes,
        templates.build_targets(template, kept),
        send_at_value,
    )


def send(
    *,
    rows: list[dict[str, Any]],
    content: str,
    subject: str = "안내",
    send_at: str = "",
) -> Sent:
    """문자를 보냅니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        content: 문안 본문
        subject: LMS 제목. SMS 로 판정되면 쓰이지 않는다
        send_at: 예약 시각(한국 시간). 빈 값이면 즉시 발송

    Returns:
        Sent: 벤더로 실제 나간 값

    Raises:
        ValueError: preview 가 걸러내는 것에 걸렸을 때. 예약 시각이 이미
            지났을 때도 여기로 옵니다 — 초안을 올린 뒤 승인까지 시간이
            흐르므로, 판정은 초안 때가 아니라 **누른 때** 기준이어야 합니다
        transport.PpurioError: 벤더가 거절했을 때
    """
    plan = preview(rows, content, send_at)
    if plan.problems:
        raise ValueError(" / ".join(plan.problems))

    # 이력에서 한 번의 발송을 묶는 키다. 벤더가 돌려주는 messageKey 는 빠질 수
    # 있어 그 자리에 쓸 수 없다 -- 우리가 만드는 값이라 늘 있다.
    ref_key = uuid.uuid4().hex
    payload = {
        "messageType": plan.message_type,
        "content": plan.template,
        # duplicateFlag·refKey 는 벤더 필수 필드입니다. 하나라도 빠지면
        # 400 code 2000 으로 요청이 통째로 거절됩니다 — 8/21 실측이고,
        # 슬랙 카드는 멀쩡히 나와서 보낸 줄 알았습니다.
        # 중복 번호는 preview 가 이미 접으므로 duplicateFlag 값은 결과를 바꾸지 않습니다.
        "duplicateFlag": "Y",
        "refKey": ref_key,
        "targetCount": len(plan.rows),
        "targets": plan.targets,
    }
    if plan.message_type != "SMS":
        payload["subject"] = subject
    if plan.send_at:
        payload["sendTime"] = plan.send_at.strftime(SEND_TIME_FORMAT)

    result = transport.send(payload)
    if str(result.get("code")) != "1000":
        raise transport.PpurioError(200, result)

    return Sent(
        sent=len(plan.rows),
        ref_key=ref_key,
        sender=transport.sender(),
        message_key=result.get("messageKey"),
        message_type=plan.message_type,
        send_at=plan.send_at,
        content=plan.template,
        targets=plan.targets,
    )
