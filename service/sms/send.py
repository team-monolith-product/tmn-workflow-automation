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

from typing import Any

from service.sms import ledger, templates, transport


def send_campaign(
    *,
    campaign: str,
    rows: list[dict[str, Any]],
    template_name: str | None = None,
    content: str | None = None,
    requested_by: str,
    entrypoint: str,
    subject: str | None = None,
    **vendor: Any,
) -> dict[str, Any]:
    """한 캠페인을 발송합니다. 이미 보낸 수신자는 자동으로 빠집니다.

    vendor 로 넘긴 키는 뿌리오 payload 에 그대로 실립니다(sendTime 등).
    우리가 모르는 벤더 옵션도 이 경로로 통과합니다.

    Args:
        campaign: 발송 건 식별자. 재발송은 다른 값을 쓴다
        rows: to·name·var1~var8 을 담은 수신자 목록
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)
        requested_by: 시킨 사람 이메일. 도구 인자가 아니라 인증에서 온 값
        entrypoint: slack · mcp · script
        subject: LMS 제목. 생략하면 campaign 을 쓴다
        **vendor: 뿌리오 payload 에 그대로 실을 추가 필드

    Returns:
        dict[str, Any]: requested·skipped·sent·code·message_key·message_type

    Raises:
        ValueError: 문안이 LMS 한도를 넘을 때
        transport.PpurioError: 벤더 호출이 실패했을 때 (자리는 '실패'로 표시)
    """
    template = templates.resolve(template_name, content)
    normalized = [{**row, "to": templates.normalize_phone(row["to"])} for row in rows]
    message_type = templates.decide_message_type(template, normalized)

    ws = ledger.open_ledger()
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
        dict[str, Any]: message_type·max_bytes·targets·sample
    """
    template = templates.resolve(template_name, content)
    normalized = [{**row, "to": templates.normalize_phone(row["to"])} for row in rows]
    rendered = [templates.render(template, row) for row in normalized]
    return {
        "message_type": templates.decide_message_type(template, normalized),
        "max_bytes": max((templates.euckr_len(text) for text in rendered), default=0),
        "targets": len(normalized),
        "sample": rendered[0] if rendered else "",
    }


def campaign_summary(campaign: str) -> dict[str, int]:
    """캠페인 진행 현황을 셉니다.

    Args:
        campaign: 발송 건 식별자

    Returns:
        dict[str, int]: total·accepted·unknown·duplicate·failed
    """
    ws = ledger.open_ledger()
    return ledger.summarize(ledger.read_rows(ws), campaign)
