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
다른 실행이 같은 사람을 빈 칸으로 보고 또 보냅니다.

한 사람이 명단에 여러 줄일 수 있습니다(폼 재응답). 그 줄들은 한 몸으로 봅니다
— 어느 하나라도 값이 있으면 보내지 않고, 선점도 전부에 씁니다. 한 줄만 보면
재응답한 사람의 새 줄이 늘 비어 있어 캠페인마다 두 번씩 나갑니다.

**선점은 완전한 잠금이 아닙니다.** 시트에는 조건부 쓰기가 없어 읽고-쓰는
사이가 열려 있고, 두 실행이 그 틈에 겹치면 둘 다 이깁니다. 서로 다른 새
캠페인 둘이 겹치면 더 나빠서, 둘 다 같은 자리를 끝으로 보고 한쪽이 다른 쪽
헤더를 덮습니다. 진입점이 여럿이 되면 한 프로세스 안에서 직렬화해야 합니다.

자리는 번호와 캠페인 이름으로 잡습니다. 행 번호나 열 번호를 들고 벤더 왕복을
건너지 않습니다 — 폼 응답 시트는 사람이 열어둔 채 응답을 지우거나 문항을
추가하고, 그 사이 위치는 밀립니다.
"""

import datetime
import re
from typing import Any

from api import google_sheets
from service.sms import KST

# 명단에서 번호 열을 찾을 때 쓰는 이름 후보. 휴대폰이 대표전화보다 앞이어야
# 합니다 — '학교 대표 전화번호'가 있는 시트에서 '전화번호'를 먼저 보면
# 지역번호를 수신자 번호로 읽고 명단 전체가 대조에 실패합니다.
PHONE_HEADERS = ("휴대폰", "휴대전화", "연락처", "전화번호", "전화", "번호")

SENDING = "발송중"

# https://docs.google.com/spreadsheets/d/<id>/edit 또는 id 자체.
# 두 분기에 같은 길이 규칙을 건다. URL 쪽만 느슨하면 게시용 링크
# (/spreadsheets/d/e/2PACX-.../pubhtml)의 'e' 가 ID 로 통과한다.
_ID_IN_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_GID_IN_URL = re.compile(r"[?&#]gid=(\d+)")


class RosterLayoutError(RuntimeError):
    """명단 탭을 발송 기록에 쓸 수 없을 때 발생합니다."""


def parse_spreadsheet_ref(value: str) -> tuple[str, int | None]:
    """스프레드시트 주소에서 시트 ID 와 탭 ID 를 함께 뽑습니다.

    사람은 보통 주소창을 통째로 붙여넣습니다. 읽히지 않으면 거절합니다 —
    통과시키면 엉뚱한 ID 로 시트를 열려다 승인이 끝난 뒤에야 죽습니다.

    둘을 따로 뽑으면 한쪽에만 주소를 넘기는 조합이 생기고, 그때 탭 ID 가
    조용히 None 이 되어 명단이 아닌 첫 탭에 캠페인 열을 만듭니다.

    Args:
        value: 스프레드시트 URL 또는 ID

    Returns:
        tuple[str, int | None]: (시트 ID, 탭 ID). 주소에 gid 가 없으면 None

    Raises:
        ValueError: 시트 ID 로 읽히지 않을 때
    """
    value = value.strip()
    found = _ID_IN_URL.search(value)
    if found:
        sheet_id = found.group(1)
    elif _BARE_ID.match(value):
        sheet_id = value
    else:
        raise ValueError(f"스프레드시트 주소나 ID 로 읽히지 않습니다: {value}")

    gid = _GID_IN_URL.search(value)
    return sheet_id, int(gid.group(1)) if gid else None


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


def open_roster(
    spreadsheet_id: str, worksheet: str | None = None, gid: int | None = None
):
    """참가자 명단 탭을 엽니다.

    어느 탭이 명단인지 고르는 규칙은 문자 발송의 결정이라 여기 둡니다.
    api/ 는 gspread 를 그대로 감싸기만 합니다.

    Args:
        spreadsheet_id: 참가자 스프레드시트 ID
        worksheet: 탭 이름. 주면 gid 보다 우선한다
        gid: 탭 ID. 둘 다 없으면 첫 번째 탭

    Returns:
        gspread.Worksheet: 명단 워크시트
    """
    sh = google_sheets.open_spreadsheet(spreadsheet_id)
    if worksheet:
        return sh.worksheet(worksheet)
    if gid is not None:
        return sh.get_worksheet_by_id(gid)
    return sh.get_worksheet(0)


def _parse_roster(
    values: list[list[str]],
) -> tuple[list[str], int, dict[str, list[int]]]:
    """읽어둔 시트 값에서 헤더와 번호를 뽑습니다. 시트를 건드리지 않습니다.

    Args:
        values: get_all_values() 결과

    Returns:
        tuple: (헤더 셀, 번호 열 번호, {숫자만 남긴 번호: 그 번호가 있는 행들})

    Raises:
        RosterLayoutError: 번호로 읽을 열이 없을 때
    """
    if not values:
        raise RosterLayoutError("명단 탭이 비어 있습니다.")

    header = values[0]
    at = None
    # 이름 후보를 셀보다 바깥에서 돕니다. 셀을 먼저 돌면 앞쪽 열이 뒤쪽
    # 후보에 걸려 '번호'가 연번 열을 집습니다.
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

    by_phone: dict[str, list[int]] = {}
    for row_number, line in enumerate(values[1:], start=2):
        phone = digits(line[at]) if at < len(line) else ""
        if phone:
            by_phone.setdefault(phone, []).append(row_number)
    return header, at, by_phone


def _find_column(header: list[str], phone_at: int, campaign: str) -> int | None:
    """캠페인 열을 찾습니다. 없으면 None. 시트를 건드리지 않습니다.

    Args:
        header: _parse_roster 가 돌려준 헤더
        phone_at: 번호 열 번호
        campaign: 발송 건 식별자 (strip 된 값)

    Returns:
        int | None: 0-based 열 번호. 아직 없으면 None

    Raises:
        RosterLayoutError: 번호 열을 캠페인 열로 쓰려 할 때
    """
    for index, cell in enumerate(header):
        if cell.strip() == campaign:
            if index == phone_at:
                raise RosterLayoutError(
                    f"'{campaign}' 은 명단의 번호 열입니다. 여기에 발송 기록을 "
                    "쓰면 번호가 지워집니다. 다른 campaign 이름을 쓰세요."
                )
            return index
    return None


def _write_column(
    ws, header: list[str], campaign: str, at: int | None, rows: list[int], value: str
) -> None:
    """그 열의 여러 행에 같은 값을 씁니다. 열이 없으면 그때 만듭니다.

    쓸 것이 없으면 열도 만들지 않습니다. 조회가 열을 만들면, 번호 열을 잘못
    잡아 한 명도 대조되지 않은 실행이 남의 시트에 빈 열만 남깁니다.

    맨 뒤에 붙입니다. 중간에 끼우면 사람이 만든 수식과 조건부 서식이 밀립니다.

    Args:
        ws: 명단 워크시트
        header: _parse_roster 가 돌려준 헤더
        campaign: 발송 건 식별자 (strip 된 값)
        at: 이미 있는 캠페인 열 번호. None 이면 새로 만든다
        rows: 대상 행 번호
        value: 적을 값. 빈 문자열이면 지운다
    """
    if not rows:
        return
    if at is None:
        at = len(header)
        ws.update([[campaign]], f"{_column_letter(at)}1", value_input_option="RAW")
    letter = _column_letter(at)
    ws.batch_update(
        [{"range": f"{letter}{row}", "values": [[value]]} for row in rows],
        value_input_option="RAW",
    )


def claim(ws, campaign: str, entries: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """보낼 사람의 자리를 선점합니다.

    명단에 있고 그 캠페인 열이 빈 사람만 대상입니다. 벤더를 부르기 전에 칸을
    '발송중'으로 채웁니다 — 채우지 않고 보내면 그 사이 다른 실행이 같은 사람을
    빈 칸으로 보고 또 보냅니다.

    시트는 한 번만 읽습니다. 헤더·번호·캠페인 열 값을 따로 읽으면 그 사이
    행이 지워졌을 때 서로 다른 스냅샷을 대조하게 됩니다.

    campaign 의 앞뒤 공백을 떼는 것은 취향이 아닙니다. 시트 셀은 strip 해서
    비교하므로, 공백이 붙은 campaign 은 자기가 만든 열조차 못 찾아 매번 새
    열을 만들고 그때마다 전원에게 다시 보냅니다.

    Args:
        ws: 명단 워크시트
        campaign: 발송 건 식별자
        entries: to·name·var1~var8 을 담은 수신자 목록 (번호는 정규화된 값)

    Returns:
        tuple: (보낼 사람, 이미 끝난 사람, 발송중으로 막힌 사람, 명단에 없는 사람).
            '발송중' 은 접수 여부를 모르는 상태라 끝난 것과 갈라 돌려준다 —
            사람이 뿌리오 웹에서 확인하고 그 칸을 지워야 풀린다

    Raises:
        ValueError: entries 에 같은 번호가 두 번 있을 때
        RosterLayoutError: 번호 열이 없거나, campaign 이 번호 열 제목일 때
    """
    campaign = campaign.strip()
    phones = [digits(entry["to"]) for entry in entries]
    if len(set(phones)) != len(phones):
        raise ValueError("같은 번호가 두 번 들어 있습니다. 접어서 넘기세요.")

    values = ws.get_all_values()
    header, phone_at, by_phone = _parse_roster(values)
    at = _find_column(header, phone_at, campaign)

    won, done, blocked, missing, rows = [], [], [], [], []
    for entry, phone in zip(entries, phones):
        found = by_phone.get(phone)
        marks = (
            [_cell(values, row, at) for row in found]
            if found and at is not None
            else []
        )
        if found is None:
            missing.append(entry)
        elif any(mark.startswith(SENDING) for mark in marks):
            blocked.append(entry)
        elif any(marks):
            done.append(entry)
        else:
            won.append(entry)
            rows.extend(found)

    stamp = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    _write_column(ws, header, campaign, at, rows, f"{SENDING} {stamp}")
    return won, done, blocked, missing


def _cell(values: list[list[str]], row: int, at: int) -> str:
    """1-based 행 번호와 0-based 열 번호로 셀 값을 읽습니다."""
    line = values[row - 1]
    return line[at].strip() if at < len(line) else ""


def mark(ws, campaign: str, phones: list[str], value: str) -> None:
    """선점한 칸을 최종 값으로 바꿉니다.

    행과 열을 번호·이름으로 다시 찾습니다. 선점 때의 위치를 그대로 쓰면,
    벤더 왕복 사이에 응답이 지워지거나 폼 문항이 추가돼 열이 밀렸을 때
    남의 칸에 씁니다 — 열이 밀린 경우 번호 열 자체를 덮습니다.

    Args:
        ws: 명단 워크시트
        campaign: 발송 건 식별자
        phones: 대상 번호 (표기 무관)
        value: 적을 값. 빈 문자열이면 지워 재시도를 연다
    """
    if not phones:
        return
    campaign = campaign.strip()
    values = ws.get_all_values()
    header, phone_at, by_phone = _parse_roster(values)
    at = _find_column(header, phone_at, campaign)
    rows = [row for phone in map(digits, phones) for row in by_phone.get(phone, [])]
    _write_column(ws, header, campaign, at, rows, value)
