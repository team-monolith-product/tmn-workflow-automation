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

**선점은 완전한 잠금이 아닙니다.** 시트에는 조건부 쓰기가 없어 읽고-쓰는
사이가 열려 있고, 두 실행이 그 틈에 겹치면 둘 다 이깁니다. 경합 구간을
벤더 왕복(수십 초)에서 시트 왕복(1~2초)으로 줄일 뿐입니다. 진입점이 여럿이
되면 한 프로세스 안에서 직렬화해야 합니다.

자리는 번호와 캠페인 이름으로 잡습니다. 행 번호나 열 번호를 들고 벤더 왕복을
건너지 않습니다 — 폼 응답 시트는 사람이 열어둔 채 응답을 지우거나 문항을
추가하고, 그 사이 위치는 밀립니다. 밀린 위치에 쓰면 남의 칸을, 최악에는
번호 열 자체를 덮습니다.
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


def parse_gid(value: str) -> int | None:
    """스프레드시트 주소에서 탭 ID 를 뽑습니다. 없으면 None.

    사람이 붙여넣는 주소에는 보고 있던 탭이 이미 들어 있습니다. 이걸 버리고
    첫 번째 탭을 열면, 명단이 두 번째 탭인 시트에서 엉뚱한 탭에 캠페인 열을
    만듭니다. 그 탭에도 번호 열이 있으면 아무도 모른 채 잘못 기록됩니다.

    Args:
        value: 스프레드시트 URL 또는 ID

    Returns:
        int | None: 탭 ID. 주소에 없으면 None
    """
    found = _GID_IN_URL.search(value)
    return int(found.group(1)) if found else None


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


def parse_roster(values: list[list[str]]) -> tuple[list[str], list[dict[str, Any]]]:
    """읽어둔 시트 값에서 헤더와 번호를 뽑습니다. 시트를 건드리지 않습니다.

    Args:
        values: get_all_values() 결과

    Returns:
        tuple: (헤더 셀, [{"phone": 숫자만, "_row": 행번호}])

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

    people = []
    for row_number, line in enumerate(values[1:], start=2):
        phone = digits(line[at]) if at < len(line) else ""
        if phone:
            people.append({"phone": phone, "_row": row_number})
    return header, people


def _campaign_column(ws, header: list[str], campaign: str) -> int:
    """캠페인 열을 찾습니다. 없으면 맨 뒤에 만듭니다.

    맨 뒤에 붙입니다. 중간에 끼우면 사람이 만든 수식과 조건부 서식이 밀립니다.

    Args:
        ws: 명단 워크시트
        header: parse_roster 가 돌려준 헤더
        campaign: 발송 건 식별자 (앞뒤 공백이 없어야 한다)

    Returns:
        int: 0-based 열 번호
    """
    for index, cell in enumerate(header):
        if cell.strip() == campaign:
            return index
    at = len(header)
    ws.update([[campaign]], f"{_column_letter(at)}1", value_input_option="RAW")
    return at


def _write_column(ws, at: int, rows: list[int], value: str) -> None:
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
    ws, campaign: str, entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
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
        tuple: (보낼 사람, 이미 보낸 사람, 명단에 없는 사람)

    Raises:
        ValueError: entries 에 같은 번호가 두 번 있을 때
    """
    campaign = campaign.strip()
    phones = [digits(entry["to"]) for entry in entries]
    if len(set(phones)) != len(phones):
        raise ValueError("같은 번호가 두 번 들어 있습니다. 접어서 넘기세요.")

    values = ws.get_all_values()
    header, people = parse_roster(values)
    at = _campaign_column(ws, header, campaign)
    by_phone = {person["phone"]: person["_row"] for person in people}
    filled = {
        row_number: (line[at] if at < len(line) else "")
        for row_number, line in enumerate(values[1:], start=2)
    }

    won, already, missing = [], [], []
    for entry, phone in zip(entries, phones):
        row = by_phone.get(phone)
        if row is None:
            missing.append(entry)
        elif filled.get(row, "").strip():
            already.append(entry)
        else:
            won.append(entry)

    stamp = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    _write_column(
        ws, at, [by_phone[digits(entry["to"])] for entry in won], f"{SENDING} {stamp}"
    )
    return won, already, missing


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
    header, people = parse_roster(values)
    at = _campaign_column(ws, header, campaign)
    wanted = {digits(phone) for phone in phones}
    rows = [person["_row"] for person in people if person["phone"] in wanted]
    _write_column(ws, at, rows, value)
