"""
구글 시트를 에이전트가 읽을 모양으로 다듬습니다.

시트는 사람이 보라고 만든 것이라 그대로 넘기면 못 씁니다. 신청 응답 시트가
297행 × 25열이고, 전부 실으면 문안 한 줄 뽑으려다 컨텍스트를 다 씁니다.
그래서 **열을 골라 받고, 글자 수 상한에서 자릅니다.**

셀 값은 사람이 손으로 넣은 것이라 믿지 않습니다. 탭과 줄 나눔이 섞여 있으면
표가 한 칸씩 밀리고, 밀린 표는 엉뚱한 사람 번호로 문자를 보내게 합니다.
"""

import re
from typing import Any, NamedTuple

DEFAULT_CHAR_LIMIT = 8_000
MAX_CHAR_LIMIT = 20_000

# https://docs.google.com/spreadsheets/d/{id}/edit#gid={gid}
_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9-_]+)")
_GID = re.compile(r"[#&?]gid=([0-9]+)")
# 링크가 아니라 ID 만 붙여넣는 경우. 44자 안팎이지만 길이로 재지 않는다.
_BARE_ID = re.compile(r"^[A-Za-z0-9-_]{20,}$")


class Sheet(NamedTuple):
    """읽을 시트를 가리키는 값."""

    spreadsheet_id: str
    worksheet_id: int | None  # None 이면 첫 번째 탭


def parse_target(text: str) -> Sheet:
    """시트 링크나 ID 에서 스프레드시트와 탭을 뽑습니다.

    Args:
        text: 시트 URL 또는 스프레드시트 ID

    Returns:
        Sheet: worksheet_id 는 링크에 gid 가 없으면 None

    Raises:
        ValueError: 시트 링크로 읽을 수 없을 때
    """
    target = (text or "").strip()
    if not target:
        raise ValueError("시트 링크가 비어 있습니다.")
    if _BARE_ID.match(target):
        return Sheet(target, None)
    found = _ID.search(target)
    if not found:
        raise ValueError(f"시트 링크에서 ID 를 찾을 수 없습니다: {text}")
    gid = _GID.search(target)
    return Sheet(found.group(1), int(gid.group(1)) if gid else None)


def _clean(cell: Any) -> str:
    """셀 값을 한 줄로 만듭니다.

    탭은 열을 가르고 줄 나눔은 행을 가릅니다. 셀 안에 그것이 있으면 표가
    한 칸씩 밀리는데, 그 사고는 앞줄만 봐서는 보이지 않습니다.
    """
    text = str(cell if cell is not None else "")
    for char in "\t\r\n":
        text = text.replace(char, " ")
    return text.strip()


def pick(
    values: list[list[Any]], columns: list[str]
) -> tuple[list[str], list[list[str]]]:
    """머리행을 기준으로 열을 고르고, 빈 행을 버립니다.

    열 이름은 정확히 같은 것을 먼저 찾고, 없으면 이름을 포함하는 열을 씁니다.
    시트 머리행이 "휴대전화 번호" 처럼 길어서, 사람은 "전화" 로 부릅니다.

    Args:
        values: 시트 전체 값. 첫 행이 머리행이다
        columns: 고를 열 이름. 비우면 전부

    Returns:
        tuple: (머리행, 행 목록)

    Raises:
        ValueError: 시트가 비었거나 찾는 열이 없을 때
    """
    if not values:
        raise ValueError("시트가 비어 있습니다.")
    header = [_clean(cell) for cell in values[0]]
    if not columns:
        keep = list(range(len(header)))
    else:
        keep = []
        missing = []
        for name in columns:
            want = _clean(name)
            if want in header:
                keep.append(header.index(want))
                continue
            loose = [i for i, head in enumerate(header) if want and want in head]
            if loose:
                keep.append(loose[0])
            else:
                missing.append(name)
        if missing:
            raise ValueError(
                f"이런 열이 없습니다: {', '.join(missing)}"
                f" / 시트의 열: {', '.join(head for head in header if head)}"
            )

    rows = []
    for row in values[1:]:
        picked = [_clean(row[i]) if i < len(row) else "" for i in keep]
        # 시트 아래쪽은 빈 행이 수백 줄 이어진다. 그것까지 세면 "297명" 이 된다.
        if any(picked):
            rows.append(picked)
    return [header[i] for i in keep], rows


def render(
    header: list[str], rows: list[list[str]], char_limit: int = DEFAULT_CHAR_LIMIT
) -> str:
    """표를 탭으로 가른 글로 만듭니다. 상한을 넘으면 자릅니다.

    Args:
        header: 머리행
        rows: 행 목록
        char_limit: 돌려줄 글자 수 상한

    Returns:
        str: 첫 줄이 머리행, 마지막 줄에 총 행수
    """
    limit = max(1, min(char_limit, MAX_CHAR_LIMIT))
    lines = ["\t".join(header)]
    length = len(lines[0])
    shown = 0
    for row in rows:
        line = "\t".join(row)
        if length + len(line) + 1 > limit:
            break
        lines.append(line)
        length += len(line) + 1
        shown += 1
    # 몇 행 중 몇 행을 봤는지 안 적으면, 잘린 목록을 전부라고 믿고 문자를 보낸다.
    if shown < len(rows):
        lines.append(f"… {len(rows)}행 중 {shown}행까지 ({limit}자 상한)")
    else:
        lines.append(f"— 총 {len(rows)}행")
    return "\n".join(lines)
