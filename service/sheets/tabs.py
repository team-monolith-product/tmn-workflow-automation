"""
시트의 탭 목록과 탭마다 머리행을 읽습니다.

카탈로그 동기화와 에이전트의 list_tabs 가 함께 씁니다. **숨긴 탭을 어디까지
보여 줄지는 여기 한 곳에서 정합니다.**
"""

from api.google_sheets import get_header_rows, get_spreadsheet_metadata


def read_headers(spreadsheet_id: str, shown_gid: int | None = None) -> list[dict]:
    """탭마다 머리행(1행)만 읽습니다. 시트 하나에 READS_PER_SHEET 회가 듭니다.

    **숨긴 탭은 뺍니다.** 사람이 감춰 둔 탭의 열 이름이 query_knowledge 검색
    결과로 나가면 안 됩니다. 실측(8/26)에 "예산 1차 변경(대외비)" 같은 탭이
    있었습니다. 빼는 것은 머리행을 **읽기 전**입니다 -- 감춘 탭은 읽지도 않습니다.

    막는 것은 발견이지 읽기가 아닙니다. 링크나 gid 를 직접 준 사람에게는
    _worksheet 이 그대로 열어 줍니다. shown_gid 도 같은 이유로 숨김이어도
    넣습니다 -- 링크가 가리킨 탭은 사람이 이미 보여 준 것입니다.

    예외를 삼키지 않습니다. 머리행을 못 읽은 탭을 빈 값으로 적재하면 카탈로그가
    "이 탭에는 열이 없다"를 최신 정보인 척 들고 있게 됩니다.

    Args:
        spreadsheet_id: 스프레드시트 ID
        shown_gid: 숨김이어도 넣을 탭의 gid. 사람이 링크로 직접 가리킨 탭입니다

    Returns:
        list[dict]: id·title·header(원본 1행)
    """
    metadata = get_spreadsheet_metadata(spreadsheet_id)
    tabs = [
        entry["properties"]
        for entry in metadata.get("sheets", [])
        if not entry["properties"].get("hidden")
        or entry["properties"]["sheetId"] == shown_gid
    ]
    if not tabs:
        return []

    result = get_header_rows(spreadsheet_id, [tab["title"] for tab in tabs])
    return [
        {
            "id": tab["sheetId"],
            "title": tab["title"],
            # 빈 탭은 values 키 자체가 없습니다.
            "header": (value_range.get("values") or [[]])[0],
        }
        # strict -- 응답이 요청보다 짧으면 뒤쪽 탭이 조용히 사라지는 대신 터집니다.
        for tab, value_range in zip(tabs, result.get("valueRanges", []), strict=True)
    ]
