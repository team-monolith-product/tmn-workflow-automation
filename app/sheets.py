"""
구글 시트를 읽는 도구입니다.

명단은 대화가 아니라 시트에 있습니다. 지금까지는 사람이 번호와 이름을 스레드에
옮겨 적어야 문자를 보낼 수 있었고, 옮겨 적다 한 글자 틀리면 엉뚱한 번호로
나갔습니다. 시트를 그대로 읽어 draft_sms 에 넘기면 그 옮겨 적기가 없어집니다.

시트는 링크로도, 이름으로도 부를 수 있습니다. 사람은 URL 을 외우지 않고
"부산 만족도 시트" 라고 부릅니다.

읽기만 합니다. 쓰기 스코프를 얹으면 에이전트가 사람이 관리하는 시트를 고칠 수
있게 되고, 그건 되돌릴 방법이 없습니다.
"""

import asyncio

from langchain_core.tools import tool

from api.google_sheets import (
    get_worksheet_titles,
    get_worksheet_values,
    search_spreadsheets,
)
from service.sheets import locate, read

DESCRIPTION = f"""구글 스프레드시트를 읽습니다. 읽기만 하고 고치지 않습니다.

sheet 에는 시트 링크를 그대로 넣거나, **시트 이름 일부**를 적습니다.
이름으로 부르면 찾아서, 하나면 읽고 여럿이면 후보 목록을 돌려줍니다.
후보가 나오면 **임의로 고르지 말고 사람에게 어느 것인지 물어보십시오** --
"…의 복사본" 같은 비슷한 이름이 실제로 여럿 있고, 잘못 고르면 엉뚱한
명단으로 문자가 나갑니다.

링크에 #gid= 가 있으면 그 탭을, 없으면 첫 번째 탭을 읽습니다.

columns 에 필요한 열 이름만 적습니다 (쉼표로 구분). **되도록 적어서 쓰십시오** --
시트는 열이 수십 개라 전부 읽으면 답을 쓸 자리가 남지 않습니다. 열 이름은
머리행과 정확히 같지 않아도 되고, 포함하는 것을 찾습니다 ("전화" → "휴대전화 번호").
비우면 전체 열을 읽습니다.

char_limit 은 돌려받을 글자 수 상한입니다. 기본 {read.DEFAULT_CHAR_LIMIT},
최대 {read.MAX_CHAR_LIMIT}. 잘리면 마지막 줄에 몇 행까지 읽었는지 적힙니다 --
**잘린 목록을 전부라고 믿고 문자를 보내지 마십시오.** 행이 많아 잘릴 것 같으면
이 도구 대신 execute_python_with_chart 안에서 read_sheet 를 부르십시오.
거기서는 전량을 읽고 pandas 로 집계합니다.

돌려주는 것은 탭으로 가른 표입니다. 첫 줄이 머리행이고 마지막 줄이 행수입니다.
빈 행은 버립니다.

열 이름을 모르면 columns 를 비우고 char_limit 을 작게 줘서 머리행만 봅니다."""


def _resolve(target: str) -> locate.Found:
    """이름이면 찾고 링크면 그대로. 블로킹 호출이라 스레드에서 부릅니다."""
    return locate.locate(target, search_spreadsheets)


def get_sheet_tools() -> list:
    """구글 시트 읽기 도구를 반환합니다.

    Returns:
        list: [읽기 도구, 탭 목록 도구]
    """

    @tool(description=DESCRIPTION)
    async def read_sheet(
        sheet: str, columns: str = "", char_limit: int = read.DEFAULT_CHAR_LIMIT
    ) -> str:
        try:
            found = await asyncio.to_thread(_resolve, sheet)
        except ValueError as error:
            return str(error)
        except Exception as error:
            return f"시트를 찾지 못했습니다: {type(error).__name__} {error}"
        if found.sheet is None:
            return locate.render_candidates(found.candidates)

        # gspread 는 동기라 스레드에서 실행합니다.
        try:
            values = await asyncio.to_thread(
                get_worksheet_values,
                found.sheet.spreadsheet_id,
                found.sheet.worksheet_id,
            )
        except Exception as error:
            # 권한·삭제·탭 없음이 전부 여기로 온다. 무엇이 막혔는지 알려주지 않으면
            # 에이전트가 같은 링크로 계속 다시 시도한다.
            return (
                f"시트를 읽지 못했습니다: {type(error).__name__} {error}"
                "\n서비스 계정에 시트가 공유되어 있는지 확인이 필요합니다."
            )

        want = [name for name in columns.split(",") if name.strip()]
        try:
            header, rows = read.pick(values, want)
        except ValueError as error:
            return str(error)
        return read.render(header, rows, char_limit)

    @tool
    async def list_sheet_tabs(sheet: str) -> str:
        """구글 스프레드시트의 탭 목록을 봅니다.

        어느 탭을 읽어야 하는지 모를 때 씁니다. 돌려주는 gid 를 시트 링크의
        #gid= 에 넣으면 그 탭을 읽습니다. sheet 에는 링크나 시트 이름을 넣습니다.
        """
        try:
            found = await asyncio.to_thread(_resolve, sheet)
        except ValueError as error:
            return str(error)
        if found.sheet is None:
            return locate.render_candidates(found.candidates)
        try:
            tabs = await asyncio.to_thread(
                get_worksheet_titles, found.sheet.spreadsheet_id
            )
        except Exception as error:
            return f"시트를 읽지 못했습니다: {type(error).__name__} {error}"
        return "\n".join(f"{tab['title']}\tgid={tab['id']}" for tab in tabs)

    return [read_sheet, list_sheet_tabs]
