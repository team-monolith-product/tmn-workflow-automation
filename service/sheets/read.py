"""
구글 시트를 에이전트가 읽을 모양으로 다듬습니다.

시트는 사람이 보라고 만든 것이라 그대로 넘기면 못 씁니다. 신청 응답 시트가
297행 × 25열이라, 필요한 **열만 골라 돌려줍니다**. 받는 양이 주는 것은 아닙니다 --
구글은 언제나 표 전체를 주고, 고르는 일은 여기 메모리에서 일어납니다.

셀 값은 사람이 손으로 넣은 것이라 믿지 않습니다. 탭과 줄 나눔이 섞여 있으면
표가 한 칸씩 밀리고, 밀린 표는 엉뚱한 사람 번호로 문자를 보내게 합니다.
"""

from typing import Any, Iterable

from api.google_sheets import get_worksheet_values
from service.sheets import locate


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
    # 만들어낸 이름도 등록해야 합니다. 시트에 "성함" 과 "성함_2" 가 **둘 다**
    # 있으면, 두 번째 "성함" 이 "성함_2" 가 되어 진짜 "성함_2" 와 부딪힙니다.
    # 유일하게 만드는 함수가 유일성을 깨는 셈이라 겹치는 동안 번호를 올립니다.
    taken: set[str] = set()
    out = []
    for index, name in enumerate(header):
        base = name or f"열{index + 1}"
        unique, number = base, 1
        while unique in taken:
            number += 1
            unique = f"{base}_{number}"
        taken.add(unique)
        out.append(unique)
    return out


def _filled(values: list[list[Any]], index: int) -> int:
    """그 열에 값이 든 행이 몇 개인지 셉니다."""
    return sum(1 for row in values[1:] if index < len(row) and _clean(row[index]))


def _column_of(
    values: list[list[Any]], raw_header: list[str], unique: list[str], want: str
) -> int | None:
    """이름으로 열 하나를 고릅니다. 같은 이름이 여럿이면 값이 든 열을 고릅니다.

    **폼 응답 시트는 같은 머리행이 두 벌 있는 일이 흔합니다.** 폼에서 문항을
    지웠다 다시 만들면 옛 열이 그대로 남고 새 응답은 뒤에 붙은 열에 쌓입니다.
    앞엣것을 집으면 값이 전부 비어 "명단 0명" 이 되고, 아무에게도 문자가
    나가지 않습니다 -- 실패가 조용해서 더 위험합니다(8/21 실측).

    Args:
        values: 시트 전체 값
        raw_header: **유일화하지 않은** 머리행. 유일화한 이름으로만 찾으면 "성함" 과
            "성함_2" 가 서로 다른 이름이 되어 아래 유령 열 판정이 죽는다
        unique: 유일화한 머리행. 열을 안 고르고 읽었을 때 돌려준 이름이 이것이라,
            그것을 그대로 다시 넘기는 경로가 있다
        want: 찾는 열 이름

    Returns:
        int | None: 열 번호. 못 찾으면 None

    Raises:
        AmbiguousColumn: 하나로 좁혀지지 않을 때
    """
    if not want:
        return None
    # 전량으로 읽으면 유일화한 이름이 키가 되므로, 그것을 그대로 다시 넘기는
    # 경로가 있습니다. 그 이름이 가리키는 열을 미리 잡아 둡니다.
    alias = unique.index(want) if want in unique else None

    # 시트 머리행이 "휴대전화 번호" 처럼 길어서 사람은 "전화" 로 부릅니다.
    # 정확히 같은 것을 먼저 보고, 없을 때만 이름을 포함하는 열로 넓힙니다.
    hits = [i for i, head in enumerate(raw_header) if head == want]
    if not hits:
        if alias is not None:
            # 원본에 없는 이름이니 겹칠 것도 없습니다.
            return alias
        hits = [i for i, head in enumerate(raw_header) if want in head]
    if not hits:
        return None

    hit = _narrow(values, raw_header, hits, want)
    if alias is not None and alias not in hits:
        # 시트에 진짜 "성함_2" 열이 있는데, 두 번째 "성함" 을 유일화한 이름도
        # "성함_2" 인 경우. 조용히 고르면 전량으로 읽었을 때와 **다른 열**이 나간다.
        #
        # alias 가 hits 안에 있으면 충돌이 아닙니다 -- 같은 이름 후보 중 하나를
        # 가리키는 것이라 바로 위 _narrow 가 이미 판정했습니다.
        raise AmbiguousColumn(
            f"'{want}' 가 시트 머리행에도 있고({hit + 1}번 열), 열을 안 고르고"
            f" 읽었을 때 {alias + 1}번 열에 붙는 이름이기도 합니다."
            f" 머리행 쪽을 부르려면 '{unique[hit]}' 로 적어 주십시오."
        )
    return hit


def _narrow(
    values: list[list[Any]], raw_header: list[str], hits: list[int], want: str
) -> int:
    """걸린 열이 여럿일 때 하나로 좁힙니다. 못 좁히면 사람에게 되돌립니다."""
    if len(hits) == 1:
        return hits[0]
    if len({raw_header[i] for i in hits}) > 1:
        # 이름이 서로 다른 열이 걸렸다. 시트가 모호하면 고르지 않는데(locate)
        # 열이라고 다를 이유가 없다 -- 잘못 고르면 엉뚱한 사람 번호로 문자가 나간다.
        raise AmbiguousColumn(
            f"'{want}' 로 여러 열이 걸립니다:"
            f" {', '.join(sorted({raw_header[i] for i in hits}))}"
            " / 어느 열인지 정확히 적어 주십시오."
        )
    # 이름이 **완전히 같은** 중복은 폼 유령 열이다. 이름이 같아 사람에게
    # 되물어도 답이 없으니 여기서 갈라야 한다.
    filled = [i for i in hits if _filled(values, i)]
    if len(filled) <= 1:
        return filled[0] if filled else hits[0]

    # **유령 열이 비어 있다고 단정하면 안 된다.** 폼에서 문항을 지우기 전에 들어온
    # 응답은 옛 열에 그대로 남는다. 대신 두 열은 행 방향으로 갈린다 -- 옛 응답은
    # 옛 열에만, 새 응답은 새 열에만 있어 **같은 행에서 둘 다 차는 일이 없다.**
    # 한 행에서라도 둘 다 차 있으면 유령이 아니라 서로 다른 열이다.
    if any(sum(1 for i in filled if _cell(row, i)) > 1 for row in values[1:]):
        raise AmbiguousColumn(
            f"'{want}' 라는 열이 {', '.join(str(i + 1) for i in filled)}번에 있고"
            " 한 행에 둘 이상 값이 차 있습니다. 폼 유령 열이 아니라 서로 다른"
            " 열이라 고를 수 없습니다. 뒤엣것은 유일화한 이름(예: 이름_2)으로"
            " 부를 수 있고, 앞엣것이 필요하면 columns 없이 전량으로 읽으십시오."
        )
    # 갈렸으니 한 열이 폼 수정으로 쪼개진 것이다. 마지막에 값이 찬 쪽이 살아 있다.
    return max(filled, key=lambda i: _last_filled_row(values, i))


def _cell(row: list[Any], index: int) -> str:
    """짧은 행을 넘어가도 터지지 않게 셀 하나를 꺼냅니다."""
    return _clean(row[index]) if index < len(row) else ""


def _last_filled_row(values: list[list[Any]], index: int) -> int:
    """그 열에 값이 마지막으로 찬 행 번호. 없으면 0."""
    for number in range(len(values) - 1, 0, -1):
        if _cell(values[number], index):
            return number
    return 0


def pick(
    values: list[list[Any]], columns: list[str]
) -> tuple[list[str], list[list[str]]]:
    """머리행을 기준으로 열을 고르고, 통째로 빈 행을 버립니다.

    **열을 고르면 부른 이름 그대로 돌려줍니다.** read_sheet(columns=["성함"]) 을
    부른 코드는 그다음 줄에서 row["성함"] 을 씁니다. 유령 열이 있는 시트라고
    "성함_2" 를 돌려주면 그 줄이 KeyError 로 터지는데, 부른 쪽에는 시트에 유령
    열이 있는지 알 방법이 없습니다.

    Args:
        values: 시트 전체 값. 첫 행이 머리행이다
        columns: 고를 열 이름. 비우면 전부

    Returns:
        tuple: (머리행, 행 목록). 머리행은 중복이 없다

    Raises:
        ValueError: 시트가 비었거나 찾는 열이 없을 때
        AmbiguousColumn: 부른 이름 둘이 같은 열을 가리킬 때
    """
    # gspread 는 빈 시트에 [] 가 아니라 [[]] 을 준다(pad_values 기본값). 둘 다 막는다.
    if not values or not any(values[0]):
        raise ValueError("시트가 비어 있습니다.")
    # 열은 **원본 이름**으로 찾습니다. 유일화한 이름으로 찾으면 "성함" 이 "성함_2" 와
    # 다른 이름이 되어, 중복 중 값이 든 쪽을 고르는 판정(_column_of)이 무력해집니다.
    raw_header = [_clean(cell) for cell in values[0]]
    unique = unique_header(raw_header)
    if not columns:
        # 전부 달라고 했으니 시트에 있는 이름을 그대로 쓰되, 겹치는 것만 번호를 답니다.
        return unique, _rows(values, range(len(raw_header)))

    keep: list[int] = []
    header: list[str] = []
    missing: list[str] = []
    taken: dict[int, str] = {}
    for name in columns:
        want = _clean(name)
        hit = _column_of(values, raw_header, unique, want)
        if hit is None:
            missing.append(name)
            continue
        if hit in taken:
            if taken[hit] == want:
                # 같은 이름을 두 번 적었다. 카탈로그의 머리행 목록을 그대로 넘기면
                # 유령 열 때문에 이렇게 된다. 접는다.
                continue
            # 이름이 다른데 같은 열이면 사람이 의도한 바가 아니다. 그냥 두면
            # 머리행에 같은 이름이 두 번 들어가 행을 dict 로 접을 때 하나가 사라진다.
            raise AmbiguousColumn(
                f"'{taken[hit]}' 와 '{want}' 이 같은 열({raw_header[hit]})을"
                " 가리킵니다 / 열마다 다른 이름을 하나씩 적어 주십시오."
            )
        taken[hit] = want
        keep.append(hit)
        header.append(want)
    if missing:
        # 유일화한 이름을 보여줍니다. 원본을 보여주면 "시트의 열: 성함, 성함" 이
        # 나가는데, 그것으로는 무엇을 적어야 하는지 알 수가 없습니다.
        raise ValueError(
            f"이런 열이 없습니다: {', '.join(missing)}"
            f" / 시트의 열: {', '.join(unique)}"
        )
    return header, _rows(values, keep)


def _rows(values: list[list[Any]], keep: Iterable[int]) -> list[list[str]]:
    """고른 열만 남기고, **통째로 빈 행만** 버립니다.

    고른 칸이 비었다고 버리면 같은 탭인데 어느 열을 골랐느냐로 행 수가 갈립니다.
    실측(8/26)에 297행짜리 시트가 성함만 고르면 197행, 발송여부를 끼면 297행이
    나왔습니다 -- 뒤쪽 100행에 누가 발송여부 한 칸을 끌어내려서입니다. 그러면
    두 번 나눠 읽어 짝지을 때 다른 사람끼리 붙습니다.

    빈 이름을 거르는 것은 부르는 쪽의 일입니다. 여기서 대신 해 주면 몇 명이
    걸러졌는지가 안 보입니다.
    """
    keep = list(keep)
    return [
        [_cell(row, i) for i in keep]
        for row in values[1:]
        if any(_clean(cell) for cell in row)
    ]


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
    found = locate.locate(sheet)
    if found.sheet is None:
        raise ValueError(locate.render_candidates(found.candidates))

    values = get_worksheet_values(
        found.sheet.spreadsheet_id,
        found.sheet.worksheet_id if tab is None else tab,
    )
    if isinstance(columns, str):
        want = [name for name in columns.split(",") if name.strip()]
    else:
        want = list(columns or [])
    header, rows = pick(values, want)
    # pick 이 돌려주는 머리행에는 중복이 없습니다. 그래야 여기서 열이 안 지워집니다.
    return [dict(zip(header, row)) for row in rows]
