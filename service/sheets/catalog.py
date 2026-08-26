"""
구글 시트를 지식베이스 item 행으로 정규화합니다.

**시트 이름·탭 이름·머리행만 넣습니다. 셀 값은 넣지 않습니다.**
응답이 계속 쌓이는 시트라, 값을 적재하면 카드가 곧 낡습니다. 낡은 숫자를
진실로 믿는 것이 값을 아예 모르는 것보다 나쁩니다. 실제 값은 필요할 때
execute_python 안에서 실시간으로 읽습니다.

그래서 content_hash 의 입력도 이름·탭·머리행뿐입니다. 응답이 300건 쌓여도
해시가 그대로라 재적재가 일어나지 않습니다 -- 노션과 다른 점이고, 시트를
훨씬 싸게 따라갈 수 있는 이유입니다.
"""

import json
from datetime import datetime
from typing import Any

from service.knowledge.ingest import compute_content_hash

SOURCE = "drive_sheet"
# 카탈로그는 검색만 하므로 LLM 정제를 돌리지 않는다. raw_text 가 이미
# 머리행이라 정제가 더 붙일 것이 없다.
DISTILL_STATE = "skipped"


def build_raw_text(name: str, tabs: list[dict[str, Any]]) -> str:
    """어휘 검색이 훑을 평문을 만듭니다.

    시트 이름과 탭 이름, 머리행을 한데 잇습니다. "출석 컬럼이 있는 시트" 처럼
    **열 이름으로 찾는 것**이 이 카탈로그의 존재 이유입니다.

    Args:
        name: 스프레드시트 이름
        tabs: id·title·header 목록

    Returns:
        str: 줄바꿈으로 이은 평문
    """
    lines = [name]
    for tab in tabs:
        header = " | ".join(cell for cell in tab.get("header", []) if cell)
        lines.append(f"[{tab['title']}] {header}")
    return "\n".join(lines)


def build_row(
    data_source_id: int, file: dict[str, Any], tabs: list[dict[str, Any]]
) -> dict[str, Any]:
    """시트 하나를 item 행으로 정규화합니다.

    Args:
        data_source_id: drive_sheet 소스의 data_source.id
        file: Drive files.list 항목 (id·name·modifiedTime·webViewLink)
        tabs: id·title·header 목록

    Returns:
        dict[str, Any]: UPSERT_ITEM 바인딩 파라미터
    """
    raw_text = build_raw_text(file["name"], tabs)
    modified = datetime.fromisoformat(file["modifiedTime"].replace("Z", "+00:00"))

    return {
        "data_source_id": data_source_id,
        "external_id": file["id"],
        "url": file.get("webViewLink")
        or f"https://docs.google.com/spreadsheets/d/{file['id']}/edit",
        "title": file["name"],
        # 파일 소유자를 캐려면 호출이 한 번 더 든다. 검색에 쓰지 않으므로 비운다.
        "author": "",
        "source_created_at": modified,
        "source_updated_at": modified,
        "raw": json.dumps({"file": file, "tabs": tabs}, ensure_ascii=False),
        "raw_text": raw_text,
        "metadata": json.dumps(
            {
                "spreadsheet_id": file["id"],
                "tabs": [
                    {"gid": tab["id"], "title": tab["title"], "columns": tab["header"]}
                    for tab in tabs
                ],
            },
            ensure_ascii=False,
        ),
        # 셀이 바뀌어도 이름·탭·머리행이 그대로면 해시가 같다 -> 재적재 없음.
        "content_hash": compute_content_hash(raw_text),
        "distill_state": DISTILL_STATE,
        "distill_after": None,
    }
