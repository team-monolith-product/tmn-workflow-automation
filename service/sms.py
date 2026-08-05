"""
문자 발송 Service Layer (뿌리오)

발송 → 결과 확인 → 실패분 재발송 → 최종 보고까지를 담당한다.

뿌리오 응답의 code 1000은 '접수 성공'일 뿐 도달을 뜻하지 않는다. 최종 도달 여부는
service.ppurio_result 가 웹 발송결과 페이지에서 읽어오고, 여기서는 그 결과가
'실패'인 건만 재발송한다. '대기'(아직 결과가 안 뜬 건)는 재발송하지 않는다 —
이미 도달한 문자를 한 번 더 보내는 사고가 나기 때문이다.
"""

import asyncio
import re
import time
from typing import Awaitable, Callable

from api import ppurio
from service import ppurio_result
from service.ppurio_result import DELIVERED, FAILED, PENDING

SMS_BYTE_LIMIT = 90  # EUC-KR 기준. 초과하면 LMS(장문)로 나간다.
SEND_ROUNDS = 3  # 최초 발송 + 재발송 2회
SEND_INTERVAL_SECONDS = 0.12  # 초당 과다 호출로 막히지 않게 여유를 둔다
POLL_INTERVAL_SECONDS = 60
POLL_LIMIT = 5  # 라운드당 최대 5분까지 결과를 기다린다

ProgressCallback = Callable[[str], Awaitable[None]]


def euckr_bytes(text: str) -> int:
    """EUC-KR 기준 바이트 수를 셉니다. (뿌리오의 SMS/LMS 판정 기준)

    Args:
        text: 문자 본문

    Returns:
        int: EUC-KR 인코딩 바이트 수
    """
    return len(text.encode("euc-kr", "replace"))


def message_type(content: str) -> str:
    """본문 길이로 메시지 유형을 판정합니다.

    Args:
        content: 문자 본문

    Returns:
        str: "SMS"(90바이트 이하) 또는 "LMS"
    """
    return "SMS" if euckr_bytes(content) <= SMS_BYTE_LIMIT else "LMS"


def normalize_phone(phone: str) -> str:
    """수신번호에서 숫자만 남기고 형식을 검증합니다.

    Args:
        phone: 수신번호 (하이픈·공백 허용)

    Returns:
        str: 숫자만 남은 수신번호

    Raises:
        ValueError: 10~11자리 숫자가 아닌 경우
    """
    digits = re.sub(r"\D", "", phone)
    if not (10 <= len(digits) <= 11):
        raise ValueError(f"수신번호 형식 오류: {phone}")
    return digits


def render_content(template: str, name: str) -> str:
    """본문 템플릿의 {name} 을 수신자 이름으로 치환합니다.

    Args:
        template: 본문 템플릿
        name: 수신자 이름

    Returns:
        str: 치환된 본문
    """
    return template.replace("{name}", name)


def build_payload(phone: str, content: str, subject: str, ref_key: str) -> dict:
    """뿌리오 발송 페이로드를 만듭니다.

    Args:
        phone: 수신번호 (숫자만)
        content: 본문
        subject: LMS 제목 (SMS 에서는 무시됨)
        ref_key: 발송 추적용 참조키

    Returns:
        dict: /v1/message 페이로드
    """
    payload = {
        "account": ppurio.get_account(),
        "messageType": message_type(content),
        "from": ppurio.get_sender(),
        "content": content,
        "duplicateFlag": "Y",
        "targetCount": 1,
        "targets": [{"to": phone}],
        "refKey": ref_key,
    }
    if payload["messageType"] != "SMS":
        payload["subject"] = subject
    return payload


def send_messages(
    recipients: list[dict], template: str, subject: str, ref_prefix: str
) -> list[dict]:
    """수신자별로 문자를 발송하고 접수 결과를 반환합니다.

    Args:
        recipients: [{"name": ..., "phone": ...}] (phone 은 숫자만)
        template: 본문 템플릿 ({name} 치환)
        subject: LMS 제목
        ref_prefix: 참조키 접두사

    Returns:
        list[dict]: [{"name", "phone", "code", "description", "message_key"}]
    """
    token_response = ppurio.post_token()
    if "token" not in token_response:
        raise RuntimeError(f"뿌리오 토큰 발급 실패: {token_response}")
    token = token_response["token"]

    results = []
    for recipient in recipients:
        content = render_content(template, recipient["name"])
        payload = build_payload(
            recipient["phone"], content, subject, f"{ref_prefix}-{recipient['phone']}"
        )
        response = ppurio.post_message(token, payload)
        results.append(
            {
                "name": recipient["name"],
                "phone": recipient["phone"],
                "code": response.get("code"),
                "description": response.get("description", ""),
                "message_key": response.get("messageKey"),
            }
        )
        time.sleep(SEND_INTERVAL_SECONDS)

    return results


async def _poll_results(phones: list[str], on_progress: ProgressCallback) -> dict:
    """모든 번호의 결과가 확정될 때까지 웹 발송결과를 폴링합니다.

    Args:
        phones: 접수에 성공한 수신번호 목록
        on_progress: 진행 상황 보고 콜백

    Returns:
        dict[str, str]: 번호 -> 성공/실패/대기
    """
    statuses: dict[str, str] = {}
    for attempt in range(1, POLL_LIMIT + 1):
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        statuses = await ppurio_result.fetch_results(phones)
        resolved = [phone for phone, status in statuses.items() if status != PENDING]
        await on_progress(
            f"발송결과 확인 {attempt}/{POLL_LIMIT} — 확정 {len(resolved)}/{len(phones)}건"
        )
        if len(resolved) == len(phones):
            break
    return statuses


async def send_and_confirm(
    recipients: list[dict],
    template: str,
    subject: str,
    on_progress: ProgressCallback,
    ref_prefix: str = "sms",
) -> dict:
    """문자를 발송하고, 실패한 건을 모두 성공할 때까지 재발송합니다.

    Args:
        recipients: [{"name": ..., "phone": ...}]
        template: 본문 템플릿 ({name} 치환)
        subject: LMS 제목
        on_progress: 진행 상황 보고 콜백
        ref_prefix: 참조키 접두사

    Returns:
        dict: {"delivered": [...], "failed": [...], "unknown": [...], "rounds": int}
            delivered 는 웹 발송결과가 성공으로 확인된 건,
            failed 는 재발송을 소진하고도 실패한 건,
            unknown 은 결과가 확정되지 않아 재발송하지 않은 건이다.
    """
    pending = [
        {"name": recipient["name"], "phone": normalize_phone(recipient["phone"])}
        for recipient in recipients
    ]
    delivered: dict[str, dict] = {}
    failed: dict[str, dict] = {}
    unknown: dict[str, dict] = {}
    rounds = 0

    for round_no in range(1, SEND_ROUNDS + 1):
        rounds = round_no
        await on_progress(f"*{round_no}회차 발송* — 대상 {len(pending)}명")

        sent = await asyncio.to_thread(
            send_messages, pending, template, subject, f"{ref_prefix}-r{round_no}"
        )
        accepted = [result for result in sent if result["code"] == "1000"]
        rejected = [result for result in sent if result["code"] != "1000"]
        await on_progress(
            f"접수 성공 {len(accepted)}건 · 접수 실패 {len(rejected)}건"
            + (f"\n접수 실패 사유: {rejected[0]['description']}" if rejected else "")
        )

        statuses = (
            await _poll_results([result["phone"] for result in accepted], on_progress)
            if accepted
            else {}
        )

        retry = []
        for result in accepted:
            phone = result["phone"]
            status = statuses.get(phone, PENDING)
            entry = {"name": result["name"], "phone": phone}
            if status == DELIVERED:
                delivered[phone] = entry
                failed.pop(phone, None)
                unknown.pop(phone, None)
            elif status == FAILED:
                failed[phone] = {**entry, "reason": "발송 실패"}
                retry.append(entry)
            else:
                unknown[phone] = {**entry, "reason": "발송결과 미확정"}

        # 접수 실패는 문자가 아예 나가지 않은 것이므로 재발송해도 중복되지 않는다.
        for result in rejected:
            entry = {"name": result["name"], "phone": result["phone"]}
            failed[result["phone"]] = {
                **entry,
                "reason": f"접수 실패({result['code']}) {result['description']}",
            }
            retry.append(entry)

        if not retry:
            break
        if round_no < SEND_ROUNDS:
            await on_progress(f"실패 {len(retry)}건을 재발송합니다.")
        pending = retry

    return {
        "delivered": list(delivered.values()),
        "failed": [entry for phone, entry in failed.items() if phone not in delivered],
        "unknown": [
            entry
            for phone, entry in unknown.items()
            if phone not in delivered and phone not in failed
        ],
        "rounds": rounds,
    }


def format_report(report: dict) -> str:
    """최종 발송 결과를 슬랙 메시지로 정리합니다.

    Args:
        report: send_and_confirm 의 반환값

    Returns:
        str: 슬랙 mrkdwn 문자열
    """
    lines = [
        f"*문자 발송 완료* ({report['rounds']}회차)",
        f"• 성공 {len(report['delivered'])}건",
    ]
    if report["failed"]:
        lines.append(f"• 실패 {len(report['failed'])}건")
        lines += [
            f"    - {entry['name']} {entry['phone']} — {entry['reason']}"
            for entry in report["failed"]
        ]
    if report["unknown"]:
        lines.append(
            f"• 결과 미확정 {len(report['unknown'])}건 "
            "(중복 발송을 피하려 재발송하지 않았습니다. 뿌리오 발송결과 페이지에서 확인해 주세요)"
        )
        lines += [
            f"    - {entry['name']} {entry['phone']}" for entry in report["unknown"]
        ]
    return "\n".join(lines)
