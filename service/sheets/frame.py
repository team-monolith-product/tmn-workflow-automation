"""
파이썬 코드가 부르는 시트 읽기입니다.

도구로 읽는 것(app/sheets.py)과 다른 점은 **자르지 않는다**는 것입니다. 결과가
컨텍스트가 아니라 실행 프로세스의 메모리로 가므로 상한을 둘 이유가 없고,
오히려 두면 잘린 표로 통계를 내게 됩니다.

행을 dict 로 돌려줍니다. pd.DataFrame(rows) 한 줄이면 표가 됩니다.
"""

from typing import Any, Callable

from service.sheets import locate, read


def build_read_sheet(
    search: Callable[[str], list[dict[str, Any]]],
    fetch: Callable[..., list[list[Any]]],
) -> Callable[..., list[dict[str, str]]]:
    """코드에 주입할 read_sheet 함수를 만듭니다.

    Args:
        search: 이름으로 스프레드시트를 찾는 함수
        fetch: (spreadsheet_id, worksheet_id) 로 값을 읽는 함수

    Returns:
        Callable: 코드가 부를 read_sheet
    """

    def read_sheet(
        sheet: str, columns: str | list[str] | None = None, tab: str | int | None = None
    ) -> list[dict[str, str]]:
        """시트를 읽어 행 목록(dict)으로 돌려줍니다. 전량이고 자르지 않습니다.

        Args:
            sheet: 시트 링크·ID 또는 이름 일부
            columns: 고를 열 이름. 쉼표로 이은 문자열도 됩니다. 생략하면 전부
            tab: 탭 gid. 생략하면 링크의 gid, 그것도 없으면 첫 번째 탭

        Returns:
            list[dict]: 머리행을 키로 하는 행 목록

        Raises:
            ValueError: 시트를 하나로 좁히지 못했거나 찾는 열이 없을 때
        """
        found = locate.locate(sheet, search)
        if found.sheet is None:
            raise ValueError(locate.render_candidates(found.candidates))

        worksheet_id = found.sheet.worksheet_id
        if tab is not None:
            # 사람이 gid 를 숫자로도 문자열로도 준다.
            worksheet_id = int(tab)

        values = fetch(found.sheet.spreadsheet_id, worksheet_id)
        if isinstance(columns, str):
            want = [name for name in columns.split(",") if name.strip()]
        else:
            want = list(columns or [])
        header, rows = read.pick(values, want)
        return [dict(zip(header, row)) for row in rows]

    return read_sheet
