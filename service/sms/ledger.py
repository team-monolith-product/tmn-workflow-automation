"""
발송 기록입니다. 참가자 시트의 명단 탭에 캠페인마다 열 하나를 씁니다.

    이름 | 소속 | 휴대폰      | … | discord안내        | 8월정산안내
    홍길동 | …   | 010-…      |   | 2026-08-11 20:14   |
    김철수 | …   | 010-…      |   | 2026-08-11 20:14   | 2026-08-20 09:00

사람이 보던 그 시트에서 누가 무엇을 받았는지 바로 보입니다. 별도 이력 탭을
두면 사람은 안 보고, 안 보는 기록은 틀려도 아무도 모릅니다.

**공식 문자만 기록합니다.** 개인 CS 문자는 슬랙 스레드가 기록입니다 — 같은
사람에게 여러 번 보내는 게 정상이라 "이미 보냈으니 빼자"는 판정 자체가
틀립니다. 여기 기록하면 두 번째 CS 가 막힙니다.

중복 차단은 **그 열이 비어 있는 사람만 보낸다**는 규칙 하나입니다. 사람이
손으로 아무 값이나 적어 넣어도 "보냈다"로 읽힙니다 — 장애 중에 뿌리오 웹으로
직접 보내고 시트에 표시하는 경로가 그대로 살아 있습니다.

벤더를 부르기 전에 그 칸을 먼저 채웁니다(선점). 채우지 않고 보내면 그 사이
다른 실행이 같은 사람을 빈 칸으로 보고 또 보냅니다. 벤더가 거절하면 다시
비워 재시도를 엽니다.
"""

import datetime
import re
from typing import Any

from api import google_sheets
from service.sms import KST

# 명단에서 번호 열을 찾을 때 쓰는 이름 후보. 구체적인 것부터 봅니다 —
# 셀을 먼저 돌면 '번호'가 연번 열에 걸립니다.
PHONE_HEADERS = ("휴대폰", "연락처", "전화번호", "휴대전화", "전화", "번호")

SENDING = "발송중"

# https://docs.google.com/spreadsheets/d/<id>/edit 또는 id 자체.
# 두 분기에 같은 길이 규칙을 건다. URL 쪽만 느슨하면 게시용 링크
# (/spreadsheets/d/e/2PACX-.../pubhtml)의 'e' 가 ID 로 통과한다.
_ID_IN_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class RosterLayoutError(RuntimeError):
    """명단 탭에서 번호 열을 찾지 못했을 때 발생합니다."""


def parse_spreadsheet_id(value: str) -> str:
    """스프레드시트 주소나 ID 에서 ID 를 뽑습니다.

    사람은 보통 주소창을 통째로 붙여넣습니다. 읽히지 않으면 거절합니다 —
    통과시키면 엉뚱한 ID 로 시트를 열려다 승인이 끝난 뒤에야 죽습니다.

    Args:
        value: 스프레드시트 URL 또는 ID

    Returns:
        str: 스프레드시트 ID

    Raises:
        ValueError: 어느 쪽으로도 읽히지 않을 때
    """
    value = value.strip()
    found = _ID_IN_URL.search(value)
    if found:
        return found.group(1)
    if _BARE_ID.match(value):
        return value
    raise ValueError(f"스프레드시트 주소나 ID 로 읽히지 않습니다: {value}")


def _column_letter(index: int) -> str:
    """0-based 열 번호를 A1 표기로 바꿉니다."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def digits(phone: str) -> str:
    """대조에 쓸 번호를 만듭니다. 숫자만 남깁니다.

    우리가 쓰는 값은 정규화된 01011111111 이지만 명단에는 사람이 적은
    010-1111-1111 이 들어 있습니다. 표기를 안 눕히면 같은 사람을 못 알아봅니다.

    normalize_phone 을 쓰지 않습니다. 그건 자릿수가 틀리면 raise 하는데,
    명단 읽기는 사람이 뭘 적었든 읽어내야 합니다.

    Args:
        phone: 번호 (표기 무관)

    Returns:
        str: 숫자만 남은 번호
    """
    return re.sub(r"\D", "", phone)


def open_roster(spreadsheet_id: str, worksheet: str | None = None):
    """참가자 명단 탭을 엽니다.

    Args:
        spreadsheet_id: 참가자 스프레드시트 ID
        worksheet: 탭 이름. 생략하면 첫 번째 탭

    Returns:
        gspread.Worksheet: 명단 워크시트
    """
    return google_sheets.get_first_worksheet(spreadsheet_id, worksheet)


def read_roster(ws) -> tuple[list[str], list[dict[str, Any]]]:
    """명단을 읽어 번호와 행 번호를 뽑습니다.

    Args:
        ws: 명단 워크시트

    Returns:
        tuple: (헤더 셀, [{"phone": 숫자만, "_row": 행번호}])

    Raises:
        RosterLayoutError: 번호로 읽을 열이 없을 때
    """
    values = ws.get_all_values()
    if not values:
        raise RosterLayoutError("명단 탭이 비어 있습니다.")

    header = values[0]
    at = None
    for name in PHONE_HEADERS:
        for index, cell in enumerate(header):
            if name in cell:
                at = index
                break
        if at is not None:
            break
    if at is None:
        raise RosterLayoutError(
            f"명단에서 번호 열을 찾지 못했습니다: {header}\n"
            f"열 제목에 {' · '.join(PHONE_HEADERS)} 중 하나가 있어야 합니다."
        )

    people = []
    for row_number, line in enumerate(values[1:], start=2):
        phone = digits(line[at]) if at < len(line) else ""
        if phone:
            people.append({"phone": phone, "_row": row_number})
    return header, people


def campaign_column(ws, header: list[str], campaign: str) -> int:
    """캠페인 열을 찾습니다. 없으면 맨 뒤에 만듭니다.

    맨 뒤에 붙입니다. 중간에 끼우면 사람이 만든 수식과 조건부 서식이 밀립니다.

    Args:
        ws: 명단 워크시트
        header: read_roster 가 돌려준 헤더
        campaign: 발송 건 식별자

    Returns:
        int: 0-based 열 번호
    """
    for index, cell in enumerate(header):
        if cell.strip() == campaign:
            return index
    at = len(header)
    ws.update([[campaign]], f"{_column_letter(at)}1", value_input_option="RAW")
    return at


def column_values(ws, at: int) -> dict[int, str]:
    """그 열의 행별 값을 읽습니다.

    Args:
        ws: 명단 워크시트
        at: 0-based 열 번호

    Returns:
        dict[int, str]: 행 번호 -> 값
    """
    values = ws.get_all_values()
    return {
        row_number: (line[at] if at < len(line) else "")
        for row_number, line in enumerate(values[1:], start=2)
    }


def write_column(ws, at: int, rows: list[int], value: str) -> None:
    """그 열의 여러 행에 같은 값을 씁니다.

    Args:
        ws: 명단 워크시트
        at: 0-based 열 번호
        rows: 대상 행 번호
        value: 적을 값. 빈 문자열이면 지운다
    """
    if not rows:
        return
    letter = _column_letter(at)
    ws.batch_update(
        [{"range": f"{letter}{row}", "values": [[value]]} for row in rows],
        value_input_option="RAW",
    )


def claim(
    ws, header: list[str], campaign: str, entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """보낼 사람의 자리를 선점합니다.

    명단에 있고 그 캠페인 열이 빈 사람만 대상입니다. 벤더를 부르기 전에 칸을
    '발송중'으로 채웁니다 — 채우지 않고 보내면 그 사이 다른 실행이 같은 사람을
    빈 칸으로 보고 또 보냅니다.

    Args:
        ws: 명단 워크시트
        header: read_roster 가 돌려준 헤더
        campaign: 발송 건 식별자
        entries: to·name·var1~var8 을 담은 수신자 목록 (번호는 정규화된 값)

    Returns:
        tuple: (보낼 사람, 이미 보낸 사람, 명단에 없는 사람).
            보낼 사람에는 _row 가 붙는다

    Raises:
        ValueError: entries 에 같은 번호가 두 번 있을 때
    """
    phones = [digits(entry["to"]) for entry in entries]
    if len(set(phones)) != len(phones):
        raise ValueError("같은 번호가 두 번 들어 있습니다. 접어서 넘기세요.")

    _, people = read_roster(ws)
    by_phone = {person["phone"]: person["_row"] for person in people}

    at = campaign_column(ws, header, campaign)
    filled = column_values(ws, at)

    won, already, missing = [], [], []
    for entry, phone in zip(entries, phones):
        row = by_phone.get(phone)
        if row is None:
            missing.append(entry)
        elif filled.get(row, "").strip():
            already.append(entry)
        else:
            won.append({**entry, "_row": row})

    stamp = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    write_column(ws, at, [item["_row"] for item in won], f"{SENDING} {stamp}")
    return won, already, missing


def mark(ws, header: list[str], campaign: str, rows: list[int], value: str) -> None:
    """선점한 칸을 최종 값으로 바꿉니다.

    Args:
        ws: 명단 워크시트
        header: read_roster 가 돌려준 헤더
        campaign: 발송 건 식별자
        rows: 대상 행 번호
        value: 적을 값. 빈 문자열이면 지워 재시도를 연다
    """
    write_column(ws, campaign_column(ws, header, campaign), rows, value)
