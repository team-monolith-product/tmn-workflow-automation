"""
발송 게이트입니다. 감사와 멱등성만 담당하고 벤더는 모릅니다.

핵심은 순서입니다.

    ① 명단의 캠페인 열에서 빈 칸인 사람만 골라 '발송중'으로 선점한다
    ② 벤더 호출 (캠페인 전체가 요청 1회)
    ③ 그 칸을 발송 시각으로 바꾼다

보내고 기록하면 ②와 ③ 사이가 경합 구간이 되어, 슬랙과 MCP 가 동시에 돌 때
같은 사람에게 두 번 갑니다. 먼저 자리를 잡고 보내면 진 쪽은 발송하지 않습니다.

②에서 벤더가 명시적으로 거절하면(HTTP 오류·code≠1000) 칸을 다시 비워
재시도를 열어둡니다. 접수되지 않은 것이 확실하기 때문입니다.

타임아웃이나 연결 끊김은 다릅니다. 벤더가 이미 접수하고 응답만 못 돌려줬을 수
있으므로 '발송중'을 그대로 남긴 채 터뜨립니다. 값이 있는 칸은 발송 대상에서
빠지므로 재시도가 막히고, 사람이 뿌리오 웹에서 확인하게 됩니다. 여기서
칸을 비우면 다음 시도가 같은 사람에게 두 번 보냅니다.
"""

import datetime
from typing import Any

from service.sms import KST, ledger, templates, transport

# 뿌리오는 이보다 가까운 예약을 거부합니다.
MIN_RESERVE_SECONDS = 180


def _normalize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """번호를 정규화하고 같은 번호는 하나로 접습니다.

    명단에 같은 사람이 두 번 들어오는 일이 실제로 있습니다. 접지 않으면
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
    worksheet: str | None = None,
    gid: int | None = None,
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
        worksheet: 명단 탭 이름. 주면 gid 보다 우선한다
        gid: 명단 탭 ID. worksheet 와 둘 다 없으면 첫 번째 탭
        send_at: 예약 발송 시각. 생략하면 즉시. 최소 3분 뒤여야 한다
        **vendor: 뿌리오 payload 에 그대로 실을 추가 필드

    Returns:
        dict[str, Any]: requested·skipped·missing·sent·sent_to·code·
            message_key·message_type

    Raises:
        ValueError: 문안·번호·길이·예약 시각에 문제가 있을 때. 넷 다 시트를
            건드리기 전에 터지므로 원장이 더러워지지 않는다. 한 번에 다 보려면
            호출부가 먼저 check 를 돌린다
        transport.PpurioError: 벤더가 거절했을 때 (자리는 '실패'로 표시)
    """
    template = templates.resolve(template_name, content)
    normalized = _normalize(rows)
    message_type = templates.decide_message_type(template, normalized)
    if send_at is not None:
        vendor["sendTime"] = reserve_time(send_at)

    ws = ledger.open_roster(spreadsheet_id, worksheet, gid)
    header, _ = ledger.read_roster(ws)
    won, already, missing = ledger.claim(ws, header, campaign, normalized)

    if not won:
        return {
            "requested": len(normalized),
            "skipped": len(already),
            "missing": [entry["to"] for entry in missing],
            "sent": 0,
            "sent_to": [],
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
    except transport.PpurioError:
        # 벤더가 거절한 것이 확실할 때만 선점을 풀어 재시도를 연다. 타임아웃은
        # 접수됐을 수 있으므로 '발송중' 을 남긴 채 터뜨린다 — 사람이 확인한다.
        ledger.mark(ws, header, campaign, claimed_rows, "")
        raise

    code = result.get("code")
    if code != "1000":
        ledger.mark(ws, header, campaign, claimed_rows, "")
        raise transport.PpurioError(200, result)

    stamp = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    ledger.mark(ws, header, campaign, claimed_rows, stamp)

    return {
        "requested": len(normalized),
        "skipped": len(already),
        "missing": [entry["to"] for entry in missing],
        "sent": len(won),
        "sent_to": [item["to"] for item in won],
        "code": code,
        "message_key": result.get("messageKey"),
        "message_type": message_type,
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
    # send_at 은 사람이 KST 로 적은 벽시계다. 컨테이너의 now() 는 UTC 라
    # 그대로 빼면 9시간 어긋나 이미 지난 시각도 통과한다.
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

    # 아래는 send_campaign 이 실제로 밟는 경로를 그대로 부르고 예외만 모읍니다.
    # 판정을 여기 따로 쓰면 사본이 갈라져 사람에게 보여준 것과 실제로 막히는
    # 것이 달라집니다.
    normalized = []
    for row in rows:
        try:
            normalized.append({**row, "to": templates.normalize_phone(row["to"])})
        except ValueError as error:
            problems.append(str(error))

    if normalized:
        try:
            templates.decide_message_type(template, normalized)
        except ValueError as error:
            problems.append(str(error))

    if send_at is not None:
        try:
            reserve_time(send_at)
        except ValueError as error:
            problems.append(str(error))

    return problems


def cancel_reserved(message_key: str) -> dict[str, Any]:
    """예약 발송을 취소합니다. 발송 1분 전까지만 가능합니다.

    명단의 캠페인 열은 지우지 않습니다. "예약했다가 취소했다"도 기록이고,
    지우면 같은 campaign 으로 다시 예약할 수 있게 되어 중복 차단이 풀립니다.
    정말 다시 보내야 하면 사람이 그 칸을 지웁니다.

    Args:
        message_key: 접수 시 받은 messageKey. 발송 직후 출력에 있다

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
