"""문자 발송 테스트용 가짜 워크시트.

gspread 워크시트가 노출하는 최소 인터페이스만 흉내냅니다.

value_input_option 을 실제로 해석하는 것이 이 페이크의 핵심입니다. 이걸
무시하고 파이썬 문자열을 그대로 보관하면, 시트가 01012345678 을 숫자로 바꿔
앞자리 0 을 버리는 사고를 테스트가 구조적으로 못 잡습니다.
"""

import re

_CELL = re.compile(r"([A-Z]+)(\d+)")


def _cell_index(range_name: str) -> tuple[int, int]:
    """A1 표기를 0-based (행, 열) 로 바꿉니다.

    26진수로 풉니다. 한 글자만 처리하면 Z 다음 열(AA)부터 죽는데, 운영
    명단이 이미 24열이라 캠페인 두 개면 거기에 닿습니다.

    Args:
        range_name: "A1" · "AA27" 같은 셀 주소

    Returns:
        tuple[int, int]: (0-based 행, 0-based 열)
    """
    column, row = _CELL.match(range_name).groups()
    left = (
        sum(
            (ord(char) - ord("A") + 1) * 26**power
            for power, char in enumerate(reversed(column))
        )
        - 1
    )
    return int(row) - 1, left


def _as_sheet_value(value, value_input_option: str):
    """시트가 셀에 실제로 저장할 값을 흉내냅니다.

    USER_ENTERED 는 사람이 타이핑한 것처럼 해석하므로 숫자로 보이는 문자열은
    수로 바뀌고 앞자리 0 이 사라집니다. RAW 는 준 그대로 넣습니다.

    Args:
        value: 우리가 쓰려는 값
        value_input_option: RAW 또는 USER_ENTERED

    Returns:
        시트에 남을 값
    """
    if value_input_option != "USER_ENTERED":
        return value
    text = str(value)
    if text.isdigit():
        return str(int(text))
    return value


class FakeWorksheet:
    """행 목록을 들고 있는 가짜 워크시트."""

    def __init__(self, rows: list[list] | None = None):
        self.rows: list[list] = [list(row) for row in (rows or [])]

    def get_all_values(self, **_kwargs) -> list[list]:
        return [list(row) for row in self.rows]

    def _put(self, row_index: int, column_index: int, value) -> None:
        while len(self.rows) <= row_index:
            self.rows.append([])
        line = self.rows[row_index]
        while len(line) <= column_index:
            line.append("")
        line[column_index] = value

    def update(
        self,
        values: list[list],
        range_name: str = "A1",
        value_input_option: str = "RAW",
        **_kwargs,
    ) -> None:
        # range_name 을 무시하면 어디에 쓰든 A1 부터 덮어써서, 헤더 뒤에 열을
        # 하나 붙이는 호출이 헤더 전체를 날린다.
        top, left = _cell_index(range_name)
        for row_offset, line in enumerate(values):
            for column_offset, cell in enumerate(line):
                self._put(
                    top + row_offset,
                    left + column_offset,
                    _as_sheet_value(cell, value_input_option),
                )

    def batch_update(
        self, updates: list[dict], value_input_option: str = "RAW", **_kwargs
    ) -> None:
        for update in updates:
            row, column = _cell_index(update["range"])
            self._put(
                row, column, _as_sheet_value(update["values"][0][0], value_input_option)
            )
