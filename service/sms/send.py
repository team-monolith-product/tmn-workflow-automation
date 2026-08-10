"""
발송 게이트입니다. 감사와 멱등성만 담당하고 벤더는 모릅니다.

핵심은 순서입니다.

    ① INSERT  (campaign, phone) UNIQUE 에 걸리면 그 사람은 이미 보낸 것
    ② 벤더 호출
    ③ UPDATE  message_key · accepted_code

보내고 기록하면 ②와 ③ 사이가 경합 구간이 되어, 슬랙과 MCP 가 동시에 돌 때
같은 사람에게 두 번 갑니다. 먼저 자리를 잡고 보내면 나중에 INSERT 하는 쪽이
제약에 걸려 반드시 집니다.

①을 커밋한 뒤에 ②로 넘어갑니다. 한 트랜잭션으로 묶으면 벤더 호출 직후
프로세스가 죽었을 때 행이 사라지고 문자만 나가서, 재시도가 중복 발송이 됩니다.
대신 accepted_code 가 NULL 로 남는 행이 생길 수 있는데, 이건 "보냈는지 모름"
이므로 사람이 확인해야 합니다. 조용히 다시 보내는 것보다 낫습니다.
"""

from typing import Any

import psycopg

from service.sms import templates, transport

CLAIM = """
INSERT INTO sms_send (
    campaign, phone, name, message_type, content_hash, requested_by, entrypoint
)
VALUES (
    %(campaign)s, %(phone)s, %(name)s, %(message_type)s, %(content_hash)s,
    %(requested_by)s, %(entrypoint)s
)
ON CONFLICT (campaign, phone) DO NOTHING
RETURNING id, phone
"""

MARK_ACCEPTED = """
UPDATE sms_send SET message_key = %(message_key)s, accepted_code = %(code)s
WHERE id = ANY(%(ids)s)
"""

RELEASE = "DELETE FROM sms_send WHERE id = ANY(%(ids)s)"

CAMPAIGN_SUMMARY = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE accepted_code = '1000') AS accepted,
       count(*) FILTER (WHERE accepted_code IS NULL)  AS unknown,
       count(*) FILTER (WHERE result_code IS NOT NULL AND result_code <> '0000')
           AS failed
FROM sms_send WHERE campaign = %(campaign)s
"""


def _claim(
    conn: psycopg.Connection,
    campaign: str,
    template: str,
    rows: list[dict[str, Any]],
    message_type: str,
    requested_by: str,
    entrypoint: str,
) -> list[dict[str, Any]]:
    """아직 보내지 않은 수신자만 골라 자리를 잡습니다.

    Args:
        conn: 커넥션
        campaign: 발송 건 식별자
        template: 치환 전 문안
        rows: 정규화된 수신자 목록
        message_type: SMS 또는 LMS
        requested_by: 시킨 사람 이메일
        entrypoint: slack · mcp · script

    Returns:
        list[dict[str, Any]]: 새로 잡은 행. id·phone·row 를 담는다
    """
    claimed = []
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                CLAIM,
                {
                    "campaign": campaign,
                    "phone": row["to"],
                    "name": row.get("name"),
                    "message_type": message_type,
                    "content_hash": templates.content_hash(template, row),
                    "requested_by": requested_by,
                    "entrypoint": entrypoint,
                },
            )
            got = cur.fetchone()
            if got:
                claimed.append({"id": got["id"], "row": row})
    return claimed


def send_campaign(
    conn: psycopg.Connection,
    *,
    campaign: str,
    template_name: str | None = None,
    content: str | None = None,
    rows: list[dict[str, Any]],
    requested_by: str,
    entrypoint: str,
    subject: str | None = None,
    **vendor: Any,
) -> dict[str, Any]:
    """한 캠페인을 발송합니다. 이미 보낸 수신자는 자동으로 빠집니다.

    vendor 로 넘긴 키는 뿌리오 payload 에 그대로 실립니다(sendTime 등).
    우리가 모르는 벤더 옵션도 이 경로로 통과합니다.

    Args:
        conn: 지식베이스 커넥션
        campaign: 발송 건 식별자. 재발송은 다른 값을 쓴다
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)
        rows: to·name·var1~var8 을 담은 수신자 목록
        requested_by: 시킨 사람 이메일. 도구 인자가 아니라 인증에서 온 값
        entrypoint: slack · mcp · script
        subject: LMS 제목. 생략하면 campaign 을 쓴다
        **vendor: 뿌리오 payload 에 그대로 실을 추가 필드

    Returns:
        dict[str, Any]: requested·skipped·sent·code·message_key

    Raises:
        ValueError: 문안이 LMS 한도를 넘을 때
        transport.PpurioError: 벤더 호출이 실패했을 때 (자리는 반납한다)
    """
    template = templates.resolve(template_name, content)
    normalized = [{**row, "to": templates.normalize_phone(row["to"])} for row in rows]
    message_type = templates.decide_message_type(template, normalized)

    claimed = _claim(
        conn, campaign, template, normalized, message_type, requested_by, entrypoint
    )
    conn.commit()

    if not claimed:
        return {
            "requested": len(normalized),
            "skipped": len(normalized),
            "sent": 0,
            "code": None,
            "message_key": None,
            "message_type": message_type,
        }

    ids = [item["id"] for item in claimed]
    payload = {
        "messageType": message_type,
        "content": template,
        "targetCount": len(claimed),
        "targets": templates.build_targets([item["row"] for item in claimed]),
        "refKey": campaign,
        **vendor,
    }
    if message_type != "SMS":
        payload["subject"] = subject or campaign

    try:
        result = transport.send(payload)
    except Exception:
        with conn.cursor() as cur:
            cur.execute(RELEASE, {"ids": ids})
        conn.commit()
        raise

    code = result.get("code")
    if code != "1000":
        with conn.cursor() as cur:
            cur.execute(RELEASE, {"ids": ids})
        conn.commit()
        raise transport.PpurioError(200, result)

    with conn.cursor() as cur:
        cur.execute(
            MARK_ACCEPTED,
            {"message_key": result.get("messageKey"), "code": code, "ids": ids},
        )
    conn.commit()

    return {
        "requested": len(normalized),
        "skipped": len(normalized) - len(claimed),
        "sent": len(claimed),
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

    Args:
        rows: 수신자 목록
        template_name: templates/sms/{name}.txt (content 와 택일)
        content: 즉석 문안 본문 (template_name 과 택일)

    Returns:
        dict[str, Any]: message_type·max_bytes·sample·targets
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


def campaign_summary(conn: psycopg.Connection, campaign: str) -> dict[str, Any]:
    """캠페인 진행 현황을 셉니다.

    Args:
        conn: 커넥션
        campaign: 발송 건 식별자

    Returns:
        dict[str, Any]: total·accepted·unknown·failed
    """
    with conn.cursor() as cur:
        cur.execute(CAMPAIGN_SUMMARY, {"campaign": campaign})
        return cur.fetchone() or {}
