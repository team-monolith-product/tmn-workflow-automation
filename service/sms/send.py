"""
발송 게이트입니다. 멱등성만 담당하고 벤더는 모릅니다.

    ① sms_send 에 자리를 잡는다 (DB 가 중복을 막는다)
    ② 벤더 호출 (캠페인 전체가 요청 1회)
    ③ 그 행에 sent_at 을 찍는다

보내고 기록하면 ②와 ③ 사이가 경합 구간이 되어 같은 사람에게 두 번 갑니다.
②의 실패가 "안 나간 것이 확실"인지 "모르는지"는 transport.PpurioError 가
정합니다.
"""

import datetime
from typing import Any, NamedTuple

from service.sms import KST, log, templates, transport

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
    send_at: datetime.datetime | None


def _plan(
    rows: list[dict[str, Any]],
    template_name: str | None,
    content: str | None,
    send_at: datetime.datetime | None,
) -> Plan:
    """보낼 수 있는지 판정하면서, 보낼 때 쓸 값을 같이 만듭니다.

    판정과 산출을 갈라 두면 사본이 생겨, 규칙 하나가 한쪽에만 들어가는 순간
    check·send_campaign·preview 가 서로 다른 말을 합니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)
        send_at: 예약 발송 시각. 없으면 즉시 발송

    Returns:
        Plan: 판정 결과와 발송에 쓸 값
    """
    try:
        template = templates.resolve(template_name, content)
    except (ValueError, FileNotFoundError) as error:
        return Plan([str(error)], "", [], 0, None, None, None)

    if not rows:
        return Plan(["수신자가 없습니다."], template, [], 0, None, None, None)

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
        send_at = _kst_naive(send_at)
        try:
            send_time = reserve_time(send_at)
        except ValueError as error:
            problems.append(str(error))

    return Plan(
        problems,
        template,
        folded,
        len(rows) - len(folded),
        message_type,
        send_time,
        send_at,
    )


def send_campaign(
    *,
    campaign: str | None,
    rows: list[dict[str, Any]],
    template_name: str | None = None,
    content: str | None = None,
    subject: str | None = None,
    send_at: datetime.datetime | None = None,
    channel_id: str | None = None,
    requested_by: str | None = None,
    **vendor: Any,
) -> dict[str, Any]:
    """한 캠페인을 발송합니다. 이미 보낸 수신자는 자동으로 빠집니다.

    Args:
        campaign: 발송 건 식별자. None 이면 개인 CS 라 중복 차단을 안 받는다
        rows: to·name·var1~var8 을 담은 수신자 목록
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)
        subject: LMS 제목. 생략하면 campaign 을 쓴다
        send_at: 예약 발송 시각. 생략하면 즉시. 최소 3분 뒤여야 한다
        channel_id: 어느 채널에서 시켰나
        requested_by: 누가 시켰나. 도구 인자가 아니라 인증에서 온 값
        **vendor: 뿌리오 payload 에 그대로 실을 추가 필드

    Returns:
        dict[str, Any]: requested·skipped·sent·sent_to·code·message_key·
            message_type

    Raises:
        ValueError: check 가 걸러내는 것에 걸렸을 때. DB 를 건드리기 전에 터진다
        transport.PpurioError: 벤더가 거절했을 때 (failed_at 을 찍어 재시도를 연다)
        Exception: 타임아웃·5xx 처럼 접수 여부를 모를 때. sent_at·failed_at 이
            둘 다 빈 채로 남아 재시도가 막히고 사람이 뿌리오 웹에서 확인한다
    """
    plan = _plan(rows, template_name, content, send_at)
    if plan.problems:
        raise ValueError(" / ".join(plan.problems))
    if plan.send_time is not None:
        vendor["sendTime"] = plan.send_time

    claimed = log.claim(
        campaign,
        plan.rows,
        content=plan.template,
        channel_id=channel_id,
        requested_by=requested_by,
    )
    won = [entry for entry in plan.rows if entry["to"] in claimed]
    code = message_key = None
    message_type = plan.message_type

    if won:
        ids = [claimed[entry["to"]] for entry in won]
        # 실제로 보낼 사람만으로 다시 판정한다. 전원 기준으로 정하면 이미
        # 받아서 빠진 사람의 긴 치환값 때문에 캠페인 전체가 LMS 로 올라간다.
        message_type = templates.decide_message_type(plan.template, won)
        payload = {
            "messageType": message_type,
            "content": plan.template,
            "targetCount": len(won),
            "targets": templates.build_targets(won),
            "refKey": campaign or "cs",
            **vendor,
        }
        if message_type != "SMS":
            payload["subject"] = subject or campaign or "안내"

        try:
            result = transport.send(payload)
            _require_accepted(result)
        except transport.PpurioError:
            log.mark_failed(ids)
            raise

        log.mark_sent(
            ids,
            message_key=result.get("messageKey"),
            scheduled_for=plan.send_at,
        )
        code, message_key = str(result["code"]), result.get("messageKey")

    return {
        "requested": len(plan.rows),
        "skipped": len(plan.rows) - len(won),
        "sent": len(won),
        "sent_to": [entry["to"] for entry in won],
        "code": code,
        "message_key": message_key,
        "message_type": message_type,
    }


def _require_accepted(result: dict[str, Any]) -> None:
    """벤더가 접수했는지 확인합니다.

    문자열로만 비교하면 벤더가 code 를 수로 돌려주는 순간 접수 성공이 실패로
    뒤집혀, 자리를 풀고 사람이 재실행해 두 번 나갑니다.

    Args:
        result: 뿌리오 응답

    Raises:
        transport.PpurioError: 접수코드가 1000 이 아닐 때
    """
    if str(result.get("code")) != "1000":
        raise transport.PpurioError(200, result)


def _kst_naive(when: datetime.datetime) -> datetime.datetime:
    """오프셋이 붙어 있으면 KST 벽시계로 눕힙니다.

    Args:
        when: 사람이 준 예약 시각

    Returns:
        datetime.datetime: tzinfo 없는 KST 벽시계
    """
    if when.tzinfo is None:
        return when
    return when.astimezone(KST).replace(tzinfo=None)


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
    send_at = _kst_naive(send_at)
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
    *,
    template_name: str | None = None,
    content: str | None = None,
    send_at: datetime.datetime | None = None,
) -> list[str]:
    """보내기 전에 걸릴 것들을 전부 모읍니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)
        send_at: 예약 발송 시각

    Returns:
        list[str]: 보낼 수 없는 이유. 비어 있으면 보낼 수 있다
    """
    return _plan(rows, template_name, content, send_at).problems


def cancel_reserved(message_key: str) -> dict[str, Any]:
    """예약 발송을 취소합니다. 발송 1분 전까지만 가능합니다.

    sms_send 행은 지우지 않습니다. 취소했다는 것도 기록이고, 지우면 같은
    campaign 으로 다시 예약할 수 있게 되어 중복 차단이 풀립니다.

    Args:
        message_key: 접수 시 받은 messageKey

    Returns:
        dict[str, Any]: 벤더 응답

    Raises:
        transport.PpurioError: 취소가 접수되지 않았을 때
    """
    result = transport.cancel(message_key)
    _require_accepted(result)
    return result


def preview(
    rows: list[dict[str, Any]],
    template_name: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """발송하지 않고 문안·타입·길이만 확인합니다. DB 도 벤더도 안 건드립니다.

    Args:
        rows: 수신자 목록
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)

    Returns:
        dict[str, Any]: message_type·max_bytes·targets·folded·sample
    """
    plan = _plan(rows, template_name, content, None)
    rendered = [templates.render(plan.template, row) for row in plan.rows]
    return {
        "message_type": plan.message_type,
        "max_bytes": max((templates.euckr_len(text) for text in rendered), default=0),
        "targets": len(plan.rows),
        "folded": plan.folded,
        "sample": rendered[0] if rendered else "",
    }
