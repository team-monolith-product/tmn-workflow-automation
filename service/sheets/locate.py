"""
"어느 시트냐" 를 정합니다.

사람은 URL 을 외우지 않습니다. "부산 만족도 시트" 라고 부릅니다. 그래서 링크가
오면 그대로 쓰고, 링크가 아니면 이름으로 찾습니다.

**후보가 여럿이면 고르지 않습니다.** 이름이 비슷한 시트가 실제로 여럿 있습니다 --
"…의 복사본", 기수별 사본, 정리용 사본. 하나를 골라 읽어 버리면 엉뚱한 명단으로
문자를 보내고, 문장이 자연스러워서 아무도 못 잡습니다.
"""

from typing import Any, NamedTuple

from service.sheets.read import Sheet, parse_target


class Found(NamedTuple):
    """찾은 결과. sheet 가 있으면 읽을 수 있고, 없으면 candidates 를 보여준다."""

    sheet: Sheet | None
    candidates: list[dict[str, Any]]


def locate(target: str, search: Any) -> Found:
    """링크면 그대로, 아니면 이름으로 찾습니다.

    Args:
        target: 시트 URL·ID 또는 이름 조각
        search: 이름으로 스프레드시트를 찾는 함수. id·name 목록을 돌려준다

    Returns:
        Found: 하나로 좁혀지면 sheet, 아니면 candidates

    Raises:
        ValueError: 이름으로도 못 찾았을 때
    """
    text = (target or "").strip()
    if not text:
        raise ValueError("어느 시트인지 알려주세요. 링크나 시트 이름이 필요합니다.")
    try:
        return Found(parse_target(text), [])
    except ValueError:
        pass

    hits = search(text)
    if not hits:
        raise ValueError(
            f"'{text}' 로 찾은 시트가 없습니다."
            " 이름 일부만 적어 보시고, 그래도 없으면 그 시트가 봇 서비스 계정에"
            " 공유되어 있는지 확인이 필요합니다."
        )
    if len(hits) == 1:
        return Found(Sheet(hits[0]["id"], None), hits)
    return Found(None, hits)


def render_candidates(candidates: list[dict[str, Any]], limit: int = 10) -> str:
    """후보 목록을 사람이 고를 수 있게 적습니다.

    Args:
        candidates: id·name 목록
        limit: 보여줄 개수

    Returns:
        str: 이름과 id 가 줄마다
    """
    lines = [f"{len(candidates)}개를 찾았습니다. 어느 것인지 알려주세요."]
    for item in candidates[:limit]:
        lines.append(f"· {item['name']}\t{item['id']}")
    if len(candidates) > limit:
        lines.append(f"… 외 {len(candidates) - limit}개")
    return "\n".join(lines)
