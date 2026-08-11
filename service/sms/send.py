"""
발송 게이트입니다. 감사와 멱등성만 담당하고 벤더는 모릅니다.

핵심은 순서입니다.

    ① 이력 시트에 자리를 잡는다(append + 행 번호로 승자 판정)
    ② 벤더 호출
    ③ 접수코드·messageKey 기록

보내고 기록하면 ②와 ③ 사이가 경합 구간이 되어, 슬랙과 MCP 가 동시에 돌 때
같은 사람에게 두 번 갑니다. 먼저 자리를 잡고 보내면 진 쪽은 발송하지 않습니다.

승자 판정 원리는 service/sms/ledger.py 에 있습니다. 요약하면 append 가
만들어주는 행 번호가 전체 순서이고, 같은 (캠페인, 번호) 중 살아 있는 최소
행 번호가 이깁니다.

②가 실패하면 자리를 '실패'로 표시해 재시도를 열어둡니다. 벌어질 수 있는
가장 나쁜 일은 ②와 ③ 사이에 프로세스가 죽어 접수코드가 빈 행이 남는 것인데,
이건 "보냈는지 모름"이라 사람이 확인해야 합니다. 조용히 다시 보내는 것보다
낫습니다.
"""

import datetime
from typing import Any

from service.sms import ledger, templates, transport

# 뿌리오는 이보다 가까운 예약을 거부합니다.
MIN_RESERVE_SECONDS = 180


def _normalize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """번호를 정규화하고 같은 번호는 하나로 접습니다.

    명단에 같은 사람이 두 번 들어오는 일이 실제로 있습니다(build_roster 가
    전화번호 기준 중복 제거를 별도 단계로 두고 있는 이유입니다). 접지 않으면
    ledger.claim 이 한 번호에 두 행을 만들고, 승자 행의 주인이 사라져 그 번호는
    이 캠페인에서 영영 발송되지 않습니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록

    Returns:
        list[dict[str, Any]]: 번호가 유일한 목록. 먼저 나온 항목을 남긴다
    """
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        phone = templates.normalize_phone(row["to"])
        unique.setdefault(phone, {**row, "to": phone})
    return list(unique.values())


def send_campaign(
    *,
    spreadsheet_id: str,
    campaign: str,
    rows: list[dict[str, Any]],
    template_name: str | None = None,
    content: str | None = None,
    requested_by: str,
    entrypoint: str,
    subject: str | None = None,
    send_at: datetime.datetime | None = None,
    **vendor: Any,
) -> dict[str, Any]:
    """한 캠페인을 발송합니다. 이미 보낸 수신자는 자동으로 빠집니다.

    vendor 로 넘긴 키는 뿌리오 payload 에 그대로 실립니다(sendTime 등).
    우리가 모르는 벤더 옵션도 이 경로로 통과합니다.

    Args:
        spreadsheet_id: 이력을 적을 참가자 스프레드시트
        campaign: 발송 건 식별자. 재발송은 다른 값을 쓴다
        rows: to·name·var1~var8 을 담은 수신자 목록
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)
        requested_by: 시킨 사람 이메일. 도구 인자가 아니라 인증에서 온 값
        entrypoint: slack · mcp · script
        subject: LMS 제목. 생략하면 campaign 을 쓴다
        send_at: 예약 발송 시각. 생략하면 즉시. 최소 3분 뒤여야 한다
        **vendor: 뿌리오 payload 에 그대로 실을 추가 필드

    Returns:
        dict[str, Any]: requested·skipped·sent·code·message_key·message_type

    Raises:
        ValueError: check 가 문제를 찾았을 때. 호출부가 미리 check 를 돌려
            사람에게 보여주는 게 정상 경로이고, 이건 안전망이다
        transport.PpurioError: 벤더 호출이 실패했을 때 (자리는 '실패'로 표시)
    """
    problems = check(
        rows, template_name=template_name, content=content, send_at=send_at
    )
    if problems:
        raise ValueError("\n".join(problems))

    template = templates.resolve(template_name, content)
    normalized = _normalize(rows)
    message_type = templates.decide_message_type(template, normalized)
    if send_at is not None:
        vendor["sendTime"] = reserve_time(send_at)

    ws = ledger.open_ledger(spreadsheet_id)
    won, lost = ledger.claim(
        ws, campaign, normalized, message_type, requested_by, entrypoint
    )
    if lost:
        ledger.mark(ws, lost, "중복")

    if not won:
        return {
            "requested": len(normalized),
            "skipped": len(normalized),
            "sent": 0,
            "code": None,
            "message_key": None,
            "message_type": message_type,
        }

    claimed_rows = [item["_row"] for item in won]
    payload = {
        "messageType": message_type,
        "content": template,
        "targetCount": len(won),
        "targets": templates.build_targets(won),
        "refKey": campaign,
        **vendor,
    }
    if message_type != "SMS":
        payload["subject"] = subject or campaign

    try:
        result = transport.send(payload)
    except Exception:
        ledger.mark(ws, claimed_rows, "실패")
        raise

    code = result.get("code")
    if code != "1000":
        ledger.mark(ws, claimed_rows, "실패")
        raise transport.PpurioError(200, result)

    ledger.mark(ws, claimed_rows, code, result.get("messageKey"))

    return {
        "requested": len(normalized),
        "skipped": len(normalized) - len(won),
        "sent": len(won),
        "code": code,
        "message_key": result.get("messageKey"),
        "message_type": message_type,
    }


def reserve_time(send_at: datetime.datetime) -> str:
    """예약 시각을 벤더 형식으로 바꿉니다. 판정은 check 가 합니다.

    Args:
        send_at: 예약 발송 시각

    Returns:
        str: yyyy-MM-ddTHH:mm:ss
    """
    return send_at.strftime("%Y-%m-%dT%H:%M:%S")


def check(
    rows: list[dict[str, Any]],
    *,
    template_name: str | None = None,
    content: str | None = None,
    send_at: datetime.datetime | None = None,
) -> list[str]:
    """보내기 전에 걸릴 것들을 전부 모읍니다.

    발송 경로 곳곳에서 하나씩 터뜨리면 사람이 고치고 다시 돌리고를 반복합니다.
    한 번에 다 보여주고 한 번에 고치게 합니다. 빈 목록이면 보낼 수 있습니다.

    벤더가 잡아주는 것도 여기서 먼저 봅니다. 벤더 거부는 발송 시도 뒤에야
    돌아오는데, 그때는 이미 이력 시트에 자리를 잡아 재시도가 막힙니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)
        send_at: 예약 발송 시각

    Returns:
        list[str]: 보낼 수 없는 이유. 비어 있으면 보낼 수 있다.
            중복 번호처럼 접어서 처리하는 것은 여기 담지 않고 preview 가 센다
    """
    problems: list[str] = []

    try:
        template = templates.resolve(template_name, content)
    except (ValueError, FileNotFoundError) as error:
        # 문안이 없으면 길이도 치환도 볼 수 없다.
        return [str(error)]

    if not rows:
        return ["수신자가 없습니다."]

    seen: set[str] = set()
    normalized = []
    for row in rows:
        try:
            phone = templates.normalize_phone(row["to"])
        except ValueError as error:
            problems.append(str(error))
            continue
        if phone in seen:
            continue
        seen.add(phone)
        normalized.append({**row, "to": phone})

    if normalized:
        longest = max(
            templates.euckr_len(templates.render(template, row)) for row in normalized
        )
        if longest > templates.LMS_MAX_BYTES:
            problems.append(
                f"치환 후 {longest}byte — LMS 한도 {templates.LMS_MAX_BYTES} 초과"
            )

    if send_at is not None:
        margin = (send_at - datetime.datetime.now()).total_seconds()
        if margin < MIN_RESERVE_SECONDS:
            problems.append(
                f"예약은 최소 {MIN_RESERVE_SECONDS // 60}분 뒤여야 합니다 "
                f"(지금 {margin / 60:.1f}분 뒤로 지정됨)"
            )

    return problems


def cancel_reserved(message_key: str) -> dict[str, Any]:
    """예약 발송을 취소합니다. 발송 1분 전까지만 가능합니다.

    이력 시트의 행은 지우지 않습니다. "예약했다가 취소했다"도 기록이고,
    지우면 같은 campaign 으로 다시 예약할 수 있게 되어 중복 차단이 풀립니다.

    Args:
        message_key: 접수 시 받은 messageKey. 발송이력 시트에 남아 있다

    Returns:
        dict[str, Any]: 벤더 응답
    """
    return transport.cancel(message_key)


def preview(
    rows: list[dict[str, Any]],
    template_name: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """발송하지 않고 문안·타입·길이만 확인합니다.

    시트도 벤더도 건드리지 않습니다.

    Args:
        rows: 수신자 목록
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)

    Returns:
        dict[str, Any]: message_type·max_bytes·targets·folded·sample
    """
    template = templates.resolve(template_name, content)
    normalized = _normalize(rows)
    rendered = [templates.render(template, row) for row in normalized]
    return {
        "message_type": templates.decide_message_type(template, normalized),
        "max_bytes": max((templates.euckr_len(text) for text in rendered), default=0),
        "targets": len(normalized),
        # 명단에 같은 사람이 두 번 있으면 접어서 보낸다. 발송 사고는 아니지만
        # 명단이 틀렸다는 신호라 사람에게 보여준다.
        "folded": len(rows) - len(normalized),
        "sample": rendered[0] if rendered else "",
    }


def campaign_summary(spreadsheet_id: str, campaign: str) -> dict[str, int]:
    """캠페인 진행 현황을 셉니다.

    Args:
        spreadsheet_id: 참가자 스프레드시트
        campaign: 발송 건 식별자

    Returns:
        dict[str, int]: total·accepted·unknown·duplicate·failed
    """
    ws = ledger.open_ledger(spreadsheet_id)
    return ledger.summarize(ledger.read_rows(ws), campaign)
