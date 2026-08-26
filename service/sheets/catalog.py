"""
구글 시트를 지식베이스 item 행으로 정규화합니다.

**시트 이름·탭 이름·머리행만 넣습니다. 셀 값은 넣지 않습니다.**
응답이 계속 쌓이는 시트라, 값을 적재하면 카드가 곧 낡습니다. 낡은 숫자를
진실로 믿는 것이 값을 아예 모르는 것보다 나쁩니다. 실제 값은 필요할 때
execute_python 안에서 실시간으로 읽습니다.

content_hash 는 **정제 상태를 가르는 게이트**입니다(ingest.UPSERT_ITEM 의 CASE).
이 소스는 정제를 돌리지 않으므로(distill_state 가 늘 skipped) 해시가 갈라도
바뀌는 것이 없습니다. 즉 여기서는 사실상 쓰지 않는 값이고, 셀이 바뀌었다고
UPDATE 가 도는 것을 막아 주지도 않습니다 -- upsert 는 해시와 무관하게 매번
전 컬럼을 씁니다. 94개 규모에서는 그 UPDATE 가 문제될 일이 없어 그대로 둡니다.
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


def modified_at(file: dict[str, Any]) -> datetime:
    """Drive 의 modifiedTime 을 datetime 으로. 없으면 터집니다.

    **여기 한 곳에서만 계산합니다.** 이 값이 source_updated_at 으로 저장되고,
    다음 동기화가 "안 바뀌었나" 를 판정할 때 그 저장값과 비교됩니다. 계산이 두
    군데로 갈라져 한쪽만 바뀌면 비교가 **항상 거짓**이 되어 30분마다 전량을
    다시 읽습니다 -- 예외도 안 나고 로그도 평소와 같습니다.

    files.list 의 fields 에 modifiedTime 을 명시했으므로 없을 수 없습니다.
    기본값을 두면 모든 시트가 "안 바뀜" 이 되어 카탈로그가 조용히 안 갱신됩니다.

    Args:
        file: Drive files.list 항목

    Returns:
        datetime: UTC aware
    """
    return datetime.fromisoformat(file["modifiedTime"].replace("Z", "+00:00"))


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
    modified = modified_at(file)

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
        # 이름·탭·머리행만 본다. 정제 게이트용이라 이 소스에서는 갈라도 달라질 게 없다.
        "content_hash": compute_content_hash(raw_text),
        "distill_state": DISTILL_STATE,
        "distill_after": None,
    }
