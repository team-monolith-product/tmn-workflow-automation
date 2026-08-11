"""
발송 이력 시트입니다. 참가자 스프레드시트의 '발송이력' 탭 하나입니다.

DB 가 아니라 시트인 이유는 사람이 손으로 한 줄 적을 수 있어야 하기 때문입니다.
장애 중에 사람이 뿌리오 웹으로 직접 보내면 그 줄을 우리 코드가 읽고 중복을
피합니다. DB 였다면 사람이 우회한 발송을 영영 모릅니다.

시트에는 UNIQUE 제약도 조건부 쓰기도 없습니다. 대신 append 가 만들어주는
행 번호를 순서로 씁니다.

    ① append          동시에 호출해도 각자 다른 행을 받는다
    ② 전체 재조회
    ③ 같은 (캠페인, 번호) 중 살아 있는 행의 최소 행 번호가 나면 이긴다

행 번호는 누가 읽어도 같으므로 승자가 하나로 정해집니다. 이 재조회를 지우면
중복 차단이 그대로 뚫립니다. tests/test_sms_ledger.py 가 막고 있습니다.

접수코드가 '실패'/'중복'인 행은 살아 있지 않은 것으로 봅니다. 그래야 실패한
발송을 다시 시도할 수 있습니다. 비어 있는 행은 "보냈는지 모름"이라 살아
있는 것으로 취급합니다 — 조용히 다시 보내는 것보다 사람이 확인하는 게 낫습니다.
"""

import datetime
import re
from typing import Any

from api import google_sheets

WORKSHEET = "발송이력"

HEADER = [
    "일시",
    "캠페인",
    "번호",
    "이름",
    "타입",
    "messageKey",
    "접수코드",
    "결과",
    "요청자",
    "경로",
]

# 이 값이 접수코드에 있으면 그 클레임은 죽은 것으로 본다.
DEAD_CODES = {"실패", "중복"}

CODE_COLUMN = chr(ord("A") + HEADER.index("접수코드"))
KEY_COLUMN = chr(ord("A") + HEADER.index("messageKey"))
PHONE_COLUMN = chr(ord("A") + HEADER.index("번호"))
RESULT_COLUMN = chr(ord("A") + HEADER.index("결과"))

_RANGE = re.compile(r"!\D+(\d+):")


def open_ledger(spreadsheet_id: str):
    """그 사업의 발송이력 탭을 엽니다. 없으면 만들고 헤더를 씁니다.

    시트 ID 를 인자로 받습니다. 전역 환경변수 하나로 두면 사업 채널이 여럿인데
    이력이 한 시트에 섞입니다.

    Args:
        spreadsheet_id: 참가자 스프레드시트 ID

    Returns:
        gspread.Worksheet: 발송이력 워크시트
    """
    ws = google_sheets.get_worksheet(spreadsheet_id, WORKSHEET)
    if not ws.get_all_values():
        ws.update([HEADER], "A1")
    # 번호 열을 텍스트로 고정한다. 두지 않으면 사람이 손으로 적은 01012345678 을
    # 시트가 숫자로 바꿔 앞자리 0 이 사라진다. 조건 밖에 둔다 — 사람이 탭을 먼저
    # 만들어 두면 위 분기에 걸리지 않아 서식이 영영 안 잡힌다. 멱등하다.
    ws.format(f"{PHONE_COLUMN}:{PHONE_COLUMN}", {"numberFormat": {"type": "TEXT"}})
    return ws


def read_rows(ws) -> list[dict[str, Any]]:
    """이력 전체를 행 번호와 함께 읽습니다.

    헤더 이름으로 열을 찾습니다. 사람이 열 순서를 바꿔도 깨지지 않게 하려는
    것입니다.

    Args:
        ws: 발송이력 워크시트

    Returns:
        list[dict[str, Any]]: 헤더를 키로 하고 _row 에 행 번호를 담은 목록
    """
    values = ws.get_all_values()
    if not values:
        return []
    header = values[0]
    rows = []
    for index, line in enumerate(values[1:], start=2):
        row = {
            name: (line[i] if i < len(line) else "") for i, name in enumerate(header)
        }
        row["_row"] = index
        rows.append(row)
    return rows


def ledger_key(campaign: str, phone: str) -> tuple[str, str]:
    """대조에 쓸 (캠페인, 번호) 키를 만듭니다.

    번호에서 숫자만 남깁니다. 우리가 쓰는 값은 정규화된 01011111111 이지만
    사람은 010-1111-1111 로 적습니다. 표기를 안 눕히면 손으로 적은 기록이
    대조되지 않아 그 사람에게 한 번 더 나갑니다 — 시트를 고른 이유가 바로
    그 손기록이라 여기가 무너지면 설계 전체가 무의미해집니다.

    normalize_phone 을 쓰지 않습니다. 그건 자릿수가 틀리면 raise 하는데,
    읽기 경로는 사람이 뭘 적었든 읽어내야 합니다.

    Args:
        campaign: 캠페인 이름
        phone: 번호 (표기 무관)

    Returns:
        tuple[str, str]: 대조용 키
    """
    return campaign.strip(), re.sub(r"\D", "", phone)


def owners(rows: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """(캠페인, 번호)별로 살아 있는 최소 행 번호를 찾습니다.

    Args:
        rows: read_rows 결과

    Returns:
        dict[tuple[str, str], int]: 키별 승자 행 번호
    """
    winner: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.get("접수코드") in DEAD_CODES:
            continue
        key = ledger_key(row.get("캠페인", ""), row.get("번호", ""))
        if key not in winner or row["_row"] < winner[key]:
            winner[key] = row["_row"]
    return winner


def claim(
    ws,
    campaign: str,
    entries: list[dict[str, Any]],
    message_type: str,
    requested_by: str,
    entrypoint: str,
) -> tuple[list[dict[str, Any]], list[int]]:
    """발송 대상의 자리를 잡습니다.

    Args:
        ws: 발송이력 워크시트
        campaign: 발송 건 식별자
        entries: to·name 을 담은 수신자 목록
        message_type: SMS 또는 LMS
        requested_by: 시킨 사람 이메일
        entrypoint: slack · mcp · script

    Returns:
        tuple: (이긴 항목 목록, 진 행 번호 목록). 이긴 항목에는 _row 가 붙는다

    Raises:
        ValueError: entries 에 같은 번호가 두 번 들어 있을 때
    """
    phones = [entry["to"] for entry in entries]
    if len(set(phones)) != len(phones):
        # 번호 -> 행 매핑이 덮여 승자 행의 주인이 사라집니다. 호출부가
        # 접어서 넘겨야 합니다(service.sms.send._normalize).
        raise ValueError("같은 번호가 두 번 들어 있습니다. 접어서 넘기세요.")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = [
        [
            stamp,
            campaign,
            entry["to"],
            entry.get("name", ""),
            message_type,
            "",  # messageKey
            "",  # 접수코드
            "",  # 결과 (도달 확인이 채운다)
            requested_by,
            entrypoint,
        ]
        for entry in entries
    ]
    # RAW 로 써야 한다. USER_ENTERED 면 시트가 01012345678 을 숫자로 해석해
    # 앞자리 0 을 버리고, 아래 재조회가 우리가 쓴 번호를 못 찾아 전원이 진다.
    result = ws.append_rows(
        payload, value_input_option="RAW", insert_data_option="INSERT_ROWS"
    )
    first = int(_RANGE.search(result["updates"]["updatedRange"]).group(1))
    mine = {entry["to"]: first + offset for offset, entry in enumerate(entries)}

    winner = owners(read_rows(ws))

    won, lost = [], []
    for entry in entries:
        row = mine[entry["to"]]
        if winner.get(ledger_key(campaign, entry["to"])) == row:
            won.append({**entry, "_row": row})
        else:
            lost.append(row)
    return won, lost


def mark(ws, rows: list[int], code: str, message_key: str | None = None) -> None:
    """행들의 접수코드와 messageKey 를 채웁니다.

    Args:
        ws: 발송이력 워크시트
        rows: 대상 행 번호
        code: 접수코드 (1000 · 실패 · 중복 …)
        message_key: 벤더가 발급한 키
    """
    updates = [{"range": f"{CODE_COLUMN}{row}", "values": [[code]]} for row in rows]
    if message_key:
        updates += [
            {"range": f"{KEY_COLUMN}{row}", "values": [[message_key]]} for row in rows
        ]
    ws.batch_update(updates, value_input_option="RAW")


def summarize(rows: list[dict[str, Any]], campaign: str) -> dict[str, int]:
    """캠페인 현황을 셉니다.

    Args:
        rows: read_rows 결과
        campaign: 발송 건 식별자

    Returns:
        dict[str, int]: total·accepted·unknown·duplicate·failed
    """
    mine = [row for row in rows if row.get("캠페인") == campaign]
    codes = [row.get("접수코드", "") for row in mine]
    return {
        "total": len(mine),
        "accepted": codes.count("1000"),
        "unknown": codes.count(""),
        "duplicate": codes.count("중복"),
        "failed": codes.count("실패"),
    }


def record_results(ws, campaign: str, statuses: dict[str, str]) -> None:
    """도달 결과를 씁니다. 이미 찬 칸은 건드리지 않습니다.

    폴링이 여러 번 돌아도 처음 확정된 결과가 남습니다.

    Args:
        ws: 발송이력 워크시트
        campaign: 발송 건 식별자
        statuses: {번호: 결과코드}
    """
    updates = [
        {"range": f"{RESULT_COLUMN}{row['_row']}", "values": [[statuses[row["번호"]]]]}
        for row in read_rows(ws)
        if row.get("캠페인") == campaign
        and not row.get("결과")
        and row.get("번호") in statuses
    ]
    if updates:
        ws.batch_update(updates, value_input_option="RAW")


def failed_targets(
    rows: list[dict[str, Any]], campaign: str, failed_code: str
) -> list[dict[str, str]]:
    """그 캠페인에서 도달에 실패한 수신자를 돌려줍니다.

    Args:
        rows: read_rows 결과
        campaign: 발송 건 식별자
        failed_code: 실패로 보는 결과코드

    Returns:
        list[dict[str, str]]: 재발송 대상 [{"to": 번호, "name": 이름}]
    """
    return [
        {"to": row["번호"], "name": row.get("이름", "")}
        for row in rows
        if row.get("캠페인") == campaign and row.get("결과") == failed_code
    ]
