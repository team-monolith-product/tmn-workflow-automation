"""
문자 발송 결과를 슬랙에 보여줄 문자열로 만듭니다.

슬랙 도구·승인 핸들러·MCP 셋이 같은 문구를 씁니다. 발송 도구 모듈에 두면
승인 핸들러가 그 모듈을 import 하고 그 모듈이 다시 승인 핸들러를 import 하는
순환이 생기므로, 공용으로 쓰이는 표현만 여기 둡니다.
"""

from typing import Any

SEPARATOR = "─" * 40


def render_preview(result: dict[str, Any]) -> str:
    """미리보기 결과를 사람이 읽을 형태로 만듭니다.

    Args:
        result: send.preview 결과

    Returns:
        str: 요약 + 본문
    """
    return (
        f"{result['message_type']} · 치환 후 최대 {result['max_bytes']}byte "
        f"· 대상 {result['targets']}명\n"
        f"{SEPARATOR}\n{result['sample']}\n{SEPARATOR}"
    )


def render_sent(campaign: str, result: dict[str, Any]) -> str:
    """접수 결과를 사람이 읽을 형태로 만듭니다.

    "접수"는 벤더가 받았다는 뜻이지 도달했다는 뜻이 아닙니다. 도달 여부는
    service.sms.result 가 나중에 확인합니다.

    Args:
        campaign: 발송 건 식별자
        result: send.send_campaign 결과

    Returns:
        str: 접수 요약
    """
    if result["sent"] == 0:
        return (
            f"[{campaign}] 대상 {result['requested']}명이 모두 "
            "이미 발송된 상태라 보내지 않았습니다."
        )
    return (
        f"[{campaign}] {result['message_type']} 접수 완료 — "
        f"발송 {result['sent']}명"
        + (f" · 중복 제외 {result['skipped']}명" if result["skipped"] else "")
        + f"\nmessageKey {result['message_key']}"
    )
