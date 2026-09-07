"""
문안과 수신자 데이터를 다루는 도메인 계층입니다.

치환은 뿌리오 태그를 그대로 씁니다.

    [*이름*]        targets[].name
    [*1*] ~ [*8*]   targets[].changeWord.var1 ~ var8
"""

import re
from typing import Any

import pandas as pd

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
        ValueError: 0 으로 시작하는 10~11자리가 아니면
    """
    digits = re.sub(r"[^0-9]", "", raw)
    # 자릿수만 보면 모델이 낸 1011111111(JSON 수는 선행 0 을 못 쓴다)이
    # 통과해, 유효하지 않은 번호가 그대로 벤더로 나간다.
    if not (10 <= len(digits) <= 11 and digits.startswith("0")):
        raise ValueError(f"수신번호 형식 오류: {raw}")
    return digits


def _tag_key(tag: str) -> str:
    """치환 태그 이름을 수신자 dict 의 키로 바꿉니다."""
    return "name" if tag == "이름" else f"var{tag}"


def _text(value: Any) -> str:
    """치환값을 벤더가 받는 문자열로 바꿉니다. 없으면 빈 칸입니다."""
    # 결측 판정은 pandas 에 맡긴다. pd.NA 는 비교 결과마저 NA 라
    # `value != value` 를 참·거짓으로 물으면 TypeError 로 터진다. isna 는
    # 배열에 배열을 돌려주므로 참인지 물어야 한다.
    if pd.isna(value) is True:
        return ""
    # 두 명단을 merge 하면 짝이 없는 칸 때문에 정수 열이 float 로 올라간다.
    # 그대로 두면 "3.0기 안내입니다" 가 나간다. 2**53 을 넘으면 float 이
    # 이미 정수를 정확히 못 담으므로 그럴듯한 오답을 만들지 않는다.
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return str(int(value))
    return str(value)


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
    return _TAG.sub(lambda m: _text(row.get(_tag_key(m.group(1)))), template)


def build_targets(template: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """벤더 targets 배열을 만듭니다.

    문안이 쓰는 태그는 값이 없어도 빈 문자열로 싣습니다. 키를 빼면 벤더가
    치환할 것을 못 찾아 그 사람만 `[*이름*]선생님` 을 받는데, 미리보기는
    첫 사람만 보여주므로 승인자가 그것을 못 봅니다.

    Args:
        template: 치환 태그가 남아 있는 원문
        rows: to 가 normalize_phone 을 거친 수신자 목록

    Returns:
        list[dict[str, Any]]: 뿌리오 targets 형식
    """
    used = {match.group(1) for match in _TAG.finditer(template)}
    change_keys = sorted(_tag_key(tag) for tag in used - {"이름"})
    targets = []
    for row in rows:
        target: dict[str, Any] = {"to": row["to"]}
        if "이름" in used:
            target["name"] = _text(row.get("name"))
        if change_keys:
            target["changeWord"] = {key: _text(row.get(key)) for key in change_keys}
        targets.append(target)
    return targets
