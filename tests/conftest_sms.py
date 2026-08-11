"""문자 발송 테스트용 가짜 워크시트.

gspread 워크시트가 노출하는 최소 인터페이스만 흉내냅니다. api/google_sheets.py
의 래퍼를 그대로 태워서, 래퍼까지 함께 검증되게 합니다.
"""

import re

_CELL = re.compile(r"([A-Z]+)(\d+)")


class FakeWorksheet:
    """행 목록을 들고 있는 가짜 워크시트."""

    def __init__(self, rows: list[list] | None = None):
        self.rows: list[list] = [list(row) for row in (rows or [])]

    def get_all_values(self, **_kwargs) -> list[list]:
        return [list(row) for row in self.rows]

    def update(self, values: list[list], _range_name: str = "A1", **_kwargs) -> None:
        for offset, row in enumerate(values):
            while len(self.rows) <= offset:
                self.rows.append([])
            self.rows[offset] = list(row)

    def append_rows(self, values: list[list], **_kwargs) -> dict:
        start = len(self.rows) + 1
        self.rows.extend(list(row) for row in values)
        end = len(self.rows)
        width = chr(ord("A") + max(len(row) for row in values) - 1)
        return {"updates": {"updatedRange": f"'발송이력'!A{start}:{width}{end}"}}

    def batch_update(self, updates: list[dict], **_kwargs) -> None:
        for update in updates:
            column, row = _CELL.match(update["range"]).groups()
            index = int(row) - 1
            position = ord(column) - ord("A")
            while len(self.rows) <= index:
                self.rows.append([])
            line = self.rows[index]
            while len(line) <= position:
                line.append("")
            line[position] = update["values"][0][0]
