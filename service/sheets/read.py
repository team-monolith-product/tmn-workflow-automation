"""
구글 시트를 에이전트가 읽을 모양으로 다듬습니다.

시트는 사람이 보라고 만든 것이라 그대로 넘기면 못 씁니다. 신청 응답 시트가
297행 × 25열이라, 필요한 **열만 골라** 받습니다.

여기서 자르지는 않습니다. 결과는 execute_python 안의 메모리로 가지 컨텍스트로
가지 않으므로 상한을 둘 이유가 없고, 두면 잘린 표로 통계를 내게 됩니다.

셀 값은 사람이 손으로 넣은 것이라 믿지 않습니다. 탭과 줄 나눔이 섞여 있으면
표가 한 칸씩 밀리고, 밀린 표는 엉뚱한 사람 번호로 문자를 보내게 합니다.
"""

import re
from typing import Any, NamedTuple

from api.google_sheets import get_worksheet_headers, get_worksheet_values  # noqa: F401
from api.google_sheets import list_spreadsheet_files

# https://docs.google.com/spreadsheets/d/{id}/edit#gid={gid}
_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9-_]+)")
_GID = re.compile(r"[#&?]gid=([0-9]+)")
# 링크가 아니라 ID 만 붙여넣는 경우. 구글 시트 ID 는 43~44자이고 대소문자가 섞인다.
# 길이만 보면 "2026_customer_satisfaction_survey" 같은 영문 시트 **이름**이 ID 로
# 오인돼 검색을 건너뛰고, 사람은 "공유를 확인하십시오" 대신 구글 404 를 본다.
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{40,}$")


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


class AmbiguousColumn(ValueError):
    """열 이름이 하나로 좁혀지지 않을 때. 고르지 않고 사람에게 되돌립니다."""


def unique_header(header: list[str]) -> list[str]:
    """머리행을 유일하게 만듭니다. 이름을 잃지 않는 것이 목적입니다.

    **행을 dict 로 접을 때 같은 키가 겹치면 뒤엣것만 남습니다.** 폼에서 문항을
    지웠다 다시 만든 시트는 같은 머리행이 두 벌이고, gspread 는 머리행보다
    오른쪽에 값이 있으면 빈 문자열로 패딩하므로 이름 없는 열도 흔합니다.
    둘 다 열을 소리 없이 지웁니다 -- 명단이 통째로 비어도 예외가 나지 않습니다.

    Args:
        header: 정리된 머리행

    Returns:
        list[str]: 같은 길이, 중복 없음. 이름 없는 열은 위치로, 겹친 이름은 번호로
    """
    seen: dict[str, int] = {}
    out = []
    for index, name in enumerate(header):
        base = name or f"열{index + 1}"
        if base in seen:
            seen[base] += 1
            base = f"{base}_{seen[base]}"
        else:
            seen[base] = 1
        out.append(base)
    return out


def _filled(values: list[list[Any]], index: int) -> int:
    """그 열에 값이 든 행이 몇 개인지 셉니다."""
    return sum(1 for row in values[1:] if index < len(row) and _clean(row[index]))


def _column_of(values: list[list[Any]], header: list[str], want: str) -> int | None:
    """이름으로 열 하나를 고릅니다. 같은 이름이 여럿이면 값이 든 열을 고릅니다.

    **폼 응답 시트는 같은 머리행이 두 벌 있는 일이 흔합니다.** 폼에서 문항을
    지웠다 다시 만들면 옛 열이 그대로 남고 새 응답은 뒤에 붙은 열에 쌓입니다.
    앞엣것을 집으면 값이 전부 비어 "명단 0명" 이 되고, 아무에게도 문자가
    나가지 않습니다 -- 실패가 조용해서 더 위험합니다(8/21 실측).

    Args:
        values: 시트 전체 값
        header: 정리된 머리행
        want: 찾는 열 이름

    Returns:
        int | None: 열 번호. 못 찾으면 None
    """
    if not want:
        return None
    exact = [i for i, head in enumerate(header) if head == want]
    if exact:
        if len(exact) == 1:
            return exact[0]
        # 이름이 **완전히 같은** 중복은 폼 유령 열이다. 값이 든 쪽이 진짜다.
        # 같으면 앞엣것(원래 순서를 흔들지 않는다).
        return max(exact, key=lambda i: (_filled(values, i), -i))

    loose = [i for i, head in enumerate(header) if want in head]
    if not loose:
        return None
    if len(loose) > 1:
        # 이름이 서로 다른 열이 걸렸다. 시트가 모호하면 고르지 않는데(locate)
        # 열이라고 다를 이유가 없다 -- 잘못 고르면 엉뚱한 사람 번호로 문자가 나간다.
        raise AmbiguousColumn(
            f"'{want}' 로 여러 열이 걸립니다: {', '.join(header[i] for i in loose)}"
            " / 어느 열인지 정확히 적어 주십시오."
        )
    return loose[0]


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
    # gspread 는 빈 시트에 [] 가 아니라 [[]] 을 준다(pad_values 기본값). 둘 다 막는다.
    if not values or not any(values[0]):
        raise ValueError("시트가 비어 있습니다.")
    # 열은 **원본 이름**으로 찾고, 돌려줄 때만 유일한 이름을 씁니다. 유일화한
    # 이름으로 찾으면 "성함" 이 "성함_2" 와 다른 이름이 되어, 중복 중 값이 든 쪽을
    # 고르는 판정(_column_of)이 통째로 무력해집니다.
    raw_header = [_clean(cell) for cell in values[0]]
    header = unique_header(raw_header)
    if not columns:
        keep = list(range(len(header)))
    else:
        keep = []
        missing = []
        for name in columns:
            hit = _column_of(values, raw_header, _clean(name))
            if hit is None:
                missing.append(name)
            else:
                keep.append(hit)
        if missing:
            raise ValueError(
                f"이런 열이 없습니다: {', '.join(missing)}"
                f" / 시트의 열: {', '.join(head for head in raw_header if head)}"
            )

    rows = []
    for row in values[1:]:
        picked = [_clean(row[i]) if i < len(row) else "" for i in keep]
        # 시트 아래쪽은 빈 행이 수백 줄 이어진다. 그것까지 세면 "297명" 이 된다.
        if any(picked):
            rows.append(picked)
    return [header[i] for i in keep], rows


def _worksheet_id(spreadsheet_id: str, tab: str | int) -> int:
    """탭 지정을 gid 로 바꿉니다. 숫자면 gid, 아니면 탭 이름으로 봅니다.

    카탈로그가 gid 와 탭 이름을 나란히 보여주므로 사람도 에이전트도 이름을 넣습니다.
    int(tab) 만 하면 "설문지 응답 시트1" 에 대해 파이썬 내부 메시지가 나가고,
    그걸로는 무엇을 고쳐야 하는지 알 수 없습니다.

    Args:
        spreadsheet_id: 스프레드시트 ID
        tab: gid 또는 탭 이름

    Returns:
        int: 워크시트 gid

    Raises:
        ValueError: 그런 이름의 탭이 없을 때. 시트의 탭 목록을 함께 알려준다
    """
    text = str(tab).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    tabs = get_worksheet_headers(spreadsheet_id)
    for entry in tabs:
        if entry["title"] == text:
            return entry["id"]
    raise ValueError(
        f"'{text}' 라는 탭이 없습니다."
        f" 이 시트의 탭: {', '.join(entry['title'] for entry in tabs)}"
    )


def read_sheet(
    sheet: str, columns: str | list[str] | None = None, tab: str | int | None = None
) -> list[dict[str, str]]:
    """시트를 읽어 행 목록(dict)으로 돌려줍니다. 전량이고 자르지 않습니다.

    에이전트가 쓰는 코드에 이 이름으로 주입됩니다. pd.DataFrame(rows) 한 줄이면
    표가 됩니다.

    **자르지 않습니다.** 결과가 컨텍스트가 아니라 실행 프로세스의 메모리로 가므로
    상한을 둘 이유가 없고, 오히려 두면 잘린 표로 통계를 내게 됩니다.
    다만 그 프로세스는 봇 넷과 스케줄러가 함께 든 app.py 입니다 -- 지금 대상인
    수백 행에서는 문제없지만, 아주 큰 시트를 통째로 올리면 슬랙 봇 전부가 같이
    내려갑니다. 그런 시트가 나오면 그때 행 수 상한을 답니다.

    시트를 **찾는** 것은 query_knowledge 가 합니다(카탈로그에 이름·탭·머리행이
    들어 있습니다). 여기서도 이름을 받긴 하는데, 카탈로그에 아직 안 실린 새 시트를
    바로 읽기 위한 길입니다.

    Args:
        sheet: 시트 링크·ID 또는 이름 일부
        columns: 고를 열 이름. 쉼표로 이은 문자열도 됩니다. 생략하면 전부
        tab: 탭 gid 또는 탭 이름. 생략하면 링크의 gid, 그것도 없으면 첫 번째 탭

    Returns:
        list[dict]: 머리행을 키로 하는 행 목록

    Raises:
        ValueError: 시트를 하나로 좁히지 못했거나, 찾는 열·탭이 없을 때
    """
    # 순환 import 를 피하려고 여기서 부릅니다 -- locate 가 read 를 씁니다.
    from service.sheets import locate

    found = locate.locate(sheet, list_spreadsheet_files)
    if found.sheet is None:
        raise ValueError(locate.render_candidates(found.candidates))

    worksheet_id = found.sheet.worksheet_id
    if tab is not None:
        worksheet_id = _worksheet_id(found.sheet.spreadsheet_id, tab)

    values = get_worksheet_values(found.sheet.spreadsheet_id, worksheet_id)
    if isinstance(columns, str):
        want = [name for name in columns.split(",") if name.strip()]
    else:
        want = list(columns or [])
    header, rows = pick(values, want)
    # 머리행이 유일해야 여기서 열이 지워지지 않습니다. unique_header 가 보장합니다.
    return [dict(zip(header, row)) for row in rows]
