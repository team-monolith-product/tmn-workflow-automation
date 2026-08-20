"""
문안과 수신자 데이터를 다루는 도메인 계층입니다.

문안은 코드가 아니라 templates/sms/*.txt 파일입니다. 문안을 하나 더 만들 때
파이썬을 고치지 않게 하려는 것이고, 그래야 발송 전 컨펌 대상이 "코드 리뷰"가
아니라 "텍스트 파일"이 됩니다. 앞선 구현은 문안이 f-string 으로 코드 안에
있어서, 잘못된 수강신청 링크가 들어간 채 발송되고도 리뷰에서 걸러지지
않았습니다(2026-07-29 courseSeq 정정).

치환은 뿌리오 태그를 그대로 씁니다.

    [*이름*]        targets[].name
    [*1*] ~ [*8*]   targets[].changeWord.var1 ~ var8

메시지 타입은 치환이 끝난 뒤에야 정해집니다. 이름이 긴 사람 한 명 때문에
90바이트를 넘으면 그 사람만 실패하므로, 판정은 발송 대상 전체의 최댓값으로
합니다.
"""

import pathlib
import re
from typing import Any

TEMPLATE_DIR = (
    pathlib.Path(__file__).resolve().parent.parent.parent / "templates" / "sms"
)

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
    if not 10 <= len(digits) <= 11:
        raise ValueError(f"수신번호 형식 오류: {raw}")
    return digits


def load(name: str) -> str:
    """문안 파일을 읽습니다.

    Args:
        name: 확장자를 뺀 파일명 (discord, confirm …)

    Returns:
        str: 치환 태그가 남아 있는 원문

    Raises:
        FileNotFoundError: 그런 문안이 없을 때
    """
    path = TEMPLATE_DIR / f"{name}.txt"
    if not path.is_file():
        # 저장된 문안은 아직 하나도 없다. 목록만 비워서 돌려주면 "사용 가능: "
        # 뒤가 잘린 것처럼 보여, 오타를 의심하며 있지도 않은 파일을 찾는다.
        available = ", ".join(sorted(p.stem for p in TEMPLATE_DIR.glob("*.txt")))
        raise FileNotFoundError(
            f"문안 '{name}' 없음. 사용 가능: {available}"
            if available
            else f"문안 '{name}' 없음. 저장된 문안이 하나도 없습니다 — "
            f"{TEMPLATE_DIR} 에 .txt 를 두거나 --content 로 본문을 바로 주세요."
        )
    return path.read_text(encoding="utf-8").rstrip("\n")


def resolve(name: str | None, content: str | None) -> str:
    """문안을 정합니다. 파일 이름과 본문 중 정확히 하나를 받습니다.

    반복해서 쓰는 문안은 파일로 두고 컨펌 대상을 텍스트 파일로 유지합니다.
    다만 파일만 허용하면 "이 조회 결과에 이 내용으로 보내줘"가 새 파일을 만드는
    PR 을 거쳐야 해서 슬랙에서 끝나지 않습니다. 일회성 문안은 본문을 그대로 받되,
    발송 전에 사람이 승인 카드에서 전문을 보고 누릅니다.

    Args:
        name: 문안 파일 이름
        content: 즉석 문안 본문

    Returns:
        str: 치환 태그가 남아 있는 원문

    Raises:
        ValueError: 둘 다 주거나 둘 다 안 줬을 때
    """
    # is None 으로 보면 빈 문자열이 둘 다 통과한다. --content "$MSG" 에서 MSG 가
    # 안 잡히면 빈 본문이 그대로 발송되고, 그 캠페인은 재발송도 막힌다.
    if bool(name) == bool(content):
        raise ValueError("문안 파일 이름과 본문 중 하나만 지정해야 합니다")
    return load(name) if name else content.rstrip("\n")


def render(template: str, row: dict[str, Any]) -> str:
    """치환 태그를 실제 값으로 바꿉니다.

    벤더도 같은 치환을 하므로 발송에는 쓰지 않습니다. 미리보기와 길이 판정에만
    씁니다.

    Args:
        template: load() 결과
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
        template: load() 결과
        rows: 발송 대상 전체

    Returns:
        str: "SMS" 또는 "LMS"

    Raises:
        ValueError: 가장 긴 본문이 LMS 한도를 넘을 때
    """
    longest = max((euckr_len(render(template, row)) for row in rows), default=0)
    if longest > LMS_MAX_BYTES:
        raise ValueError(f"치환 후 {longest}byte — LMS 한도 {LMS_MAX_BYTES} 초과")
    return "SMS" if longest <= SMS_MAX_BYTES else "LMS"


def build_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """벤더 targets 배열을 만듭니다.

    Args:
        rows: to·name·var1~var8 을 담은 수신자 목록

    Returns:
        list[dict[str, Any]]: 뿌리오 targets 형식
    """
    targets = []
    for row in rows:
        target: dict[str, Any] = {"to": normalize_phone(row["to"])}
        if row.get("name"):
            target["name"] = row["name"]
        change_word = {key: str(row[key]) for key in VAR_KEYS if row.get(key)}
        if change_word:
            target["changeWord"] = change_word
        targets.append(target)
    return targets
