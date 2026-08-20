"""
문안과 수신자 데이터를 다루는 도메인 계층입니다.

치환은 뿌리오 태그를 그대로 씁니다.

    [*이름*]        targets[].name
    [*1*] ~ [*8*]   targets[].changeWord.var1 ~ var8
"""

import re
from typing import Any

SMS_MAX_BYTES = 90
LMS_MAX_BYTES = 2000

VAR_KEYS = tuple(f"var{i}" for i in range(1, 9))
_TAG = re.compile(r"\[\*(이름|[1-8])\*\]")


def euckr_len(text: str) -> int:
    """EUC-KR 기준 바이트 수를 셉니다.

    이동통신 문자 길이는 EUC-KR 기준입니다. UTF-8 로 재면 한글이 3바이트라
    실제보다 길게 나옵니다.

    Args:
        text: 잴 문자열

    Returns:
        int: 바이트 수
    """
    return len(text.encode("euc-kr", "replace"))


def normalize_phone(raw: str) -> str:
    """수신번호에서 하이픈·공백을 걷어냅니다.

    뿌리오 문서에 형식 규정이 없어 우리가 맞춥니다.

    Args:
        raw: 010-1234-5678 같은 입력

    Returns:
        str: 숫자만 남은 번호

    Raises:
        ValueError: 숫자만 남겼을 때 10~11자리가 아니면
    """
    digits = re.sub(r"[^0-9]", "", raw)
    # 자릿수만 보면 모델이 낸 1011111111(JSON 수는 선행 0 을 못 쓴다)이
    # 통과해, 유효하지 않은 번호가 그대로 벤더로 나간다.
    if not (10 <= len(digits) <= 11 and digits.startswith("0")):
        raise ValueError(f"수신번호 형식 오류: {raw}")
    return digits


def render(template: str, row: dict[str, Any]) -> str:
    """치환 태그를 실제 값으로 바꿉니다.

    벤더도 같은 치환을 하므로 발송에는 쓰지 않습니다. 미리보기와 길이 판정에만
    씁니다.

    Args:
        template: 치환 태그가 남아 있는 원문
        row: to·name·var1~var8 을 담은 수신자 한 명

    Returns:
        str: 치환이 끝난 본문
    """

    def replace(match: re.Match) -> str:
        tag = match.group(1)
        key = "name" if tag == "이름" else f"var{tag}"
        return str(row.get(key) or "")

    return _TAG.sub(replace, template)


def decide_message_type(template: str, rows: list[dict[str, Any]]) -> str:
    """발송 전체에 적용할 메시지 타입을 정합니다.

    벤더는 이 값을 자동으로 올려주지 않고, 이 값을 기준으로 요금을 차감합니다.
    치환 결과가 가장 긴 수신자를 기준으로 잡아야 한 명만 실패하는 일이 없습니다.

    Args:
        template: 치환 태그가 남아 있는 원문
        rows: 발송 대상 전체

    Returns:
        str: "SMS" 또는 "LMS"

    Raises:
        ValueError: 가장 긴 본문이 LMS 한도를 넘을 때
    """
    # 벤더로 나가는 것은 치환 전 원문이다. 치환값이 태그보다 짧으면 치환 후만
    # 재서 SMS 로 판정해 놓고 90byte 넘는 원문을 보내게 된다.
    longest = max(
        euckr_len(template),
        max((euckr_len(render(template, row)) for row in rows), default=0),
    )
    if longest > LMS_MAX_BYTES:
        raise ValueError(f"치환 후 {longest}byte — LMS 한도 {LMS_MAX_BYTES} 초과")
    return "SMS" if longest <= SMS_MAX_BYTES else "LMS"


def build_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """벤더 targets 배열을 만듭니다.

    Args:
        rows: to 가 normalize_phone 을 거친 수신자 목록

    Returns:
        list[dict[str, Any]]: 뿌리오 targets 형식
    """
    targets = []
    for row in rows:
        target: dict[str, Any] = {"to": row["to"]}
        if row.get("name"):
            target["name"] = str(row["name"])
        change_word = {key: str(row[key]) for key in VAR_KEYS if row.get(key)}
        if change_word:
            target["changeWord"] = change_word
        targets.append(target)
    return targets
