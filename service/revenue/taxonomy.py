"""
매출 분류 엔진과 분류마스터 검증.

규칙은 knowledge/revenue/taxonomy.yml 에 있다. 뷰(view)마다 kind 가 다르다.
  passthrough  매출장 열 값을 그대로 버킷으로 쓴다
  rules        prefer_raw -> exceptions(위에서부터 첫 일치) -> base 순으로 판정
  enrichment   보강 테이블 값을 버킷으로 쓴다

규칙에 안 걸리면 「미분류」로 남긴다. 임의로 배정하지 않는다 — 빌드가 경고를 낸다.
"""

import re
from collections import defaultdict
from typing import Any

UNCLASSIFIED = "미분류"


def clean(value: Any) -> str:
    """셀 값을 한 줄로. 탭·연속 공백을 접는다."""
    return re.sub(r"\s+", " ", str(value).replace("\t", " ")).strip()


def rule_matches(when: dict[str, Any], tx: dict[str, Any]) -> bool:
    """when 절의 모든 조건을 만족해야 한다(AND)."""
    if "year" in when and tx["year"] != when["year"]:
        return False
    if "raw" in when and tx["category_raw"] != when["raw"]:
        return False
    if (
        "counterparty_has" in when
        and when["counterparty_has"] not in tx["counterparty"]
    ):
        return False
    if "counterparty_regex" in when and not re.search(
        when["counterparty_regex"], tx["counterparty"]
    ):
        return False
    if "item_has" in when and when["item_has"] not in tx["item"]:
        return False
    if "item_regex" in when and not re.search(when["item_regex"], tx["item"]):
        return False
    if "item_has_any" in when and not any(
        key in tx["item"] for key in when["item_has_any"]
    ):
        return False
    if "sub_any" in when and tx.get("subcategory_raw", "") not in when["sub_any"]:
        return False
    if "amount" in when and tx["amount"] != when["amount"]:
        return False
    return True


def classify(tx: dict[str, Any], view: dict[str, Any]) -> tuple[str, str | None]:
    """거래 하나를 뷰 하나로 판정한다.

    Returns:
        tuple: (버킷, 적용된 예외규칙 id). 규칙 없이 base 로 갔으면 id 는 None
    """
    if view.get("kind") == "passthrough":
        return tx.get(view["field"]) or view.get("empty", UNCLASSIFIED), None
    prefer = view.get("prefer_raw") or []
    if prefer and tx["category_raw"] in prefer:
        return tx["category_raw"], "매출장 분류 그대로"
    if view.get("kind") == "enrichment":
        value = (tx.get("enrich") or {}).get(view["field"])
        if isinstance(value, list):
            value = value[0] if value else None
        return (str(value).strip() if value else view.get("empty", "미상")), None
    for rule in view.get("exceptions", []) or []:
        if rule_matches(rule["when"], tx):
            return rule["then"], rule["id"]
    base = view.get("base", {})
    return base.get(tx["category_raw"], UNCLASSIFIED), None


def parse_taxonomy_master(
    raw: dict[str, Any], sources: dict[str, Any]
) -> dict[str, list[str]]:
    """매출장 「분류마스터」 탭 -> {대분류: [세부분류...]}.

    분류 정본은 이 표다. 코드가 이름 목록을 따로 들고 있지 않는다.
    시트에서 분류를 늘리면 다음 빌드에 그대로 따라온다.
    """
    blob = raw.get("taxonomy_tab") or {}
    rows = blob.get("rows") or []
    spec = sources["ledger"].get("taxonomy_tab") or {}
    if not rows or not spec:
        return {}

    def at(row_index: int, col_index: int) -> str:
        row = rows[row_index] if 0 <= row_index < len(rows) else []
        return clean(row[col_index]) if 0 <= col_index < len(row) else ""

    majors = []
    for row_index in range(spec["major_start_row"] - 1, len(rows)):
        value = at(row_index, spec["major_col"])
        if value:
            majors.append(value)

    details: dict[str, list[str]] = {major: [] for major in majors}
    header_row = spec["detail_header_row"] - 1
    for col_index in range(spec["detail_first_col"], spec["detail_last_col"] + 1):
        major = at(header_row, col_index)
        if not major:
            continue
        details[major] = [
            at(row_index, col_index)
            for row_index in range(spec["detail_start_row"] - 1, len(rows))
            if at(row_index, col_index)
        ]

    # 헤더에만 있고 A열 목록에 없는 대분류도 살린다. 시트가 한쪽만 갱신됐을 수 있다.
    for major in details:
        if major not in majors:
            majors.append(major)
    return {major: details.get(major, []) for major in majors}


def apply_master(views: dict[str, Any], master: dict[str, list[str]]) -> None:
    """분류 이름과 순서의 정본은 매출장이다. taxonomy.yml 의 하드코딩 목록을 덮어쓴다.

    base·exceptions 의 대상 이름은 덮어쓰지 않는다. 그래서 시트에서 대분류 이름을
    바꾸면 과거 연도가 유령 항목으로 새어 나간다 — validate_rules_against_master 가 잡는다.
    """
    if not master:
        return
    majors = list(master)
    for view in views.values():
        if view.get("prefer_raw") is not None:
            view["prefer_raw"] = majors
            view["order"] = majors
        if view.get("field") == "subcategory_raw":
            flat: list[str] = []
            for major in majors:
                for sub in master[major]:
                    if sub not in flat:
                        flat.append(sub)
            view["order"] = flat


def validate_rules_against_master(
    views: dict[str, Any], master: dict[str, list[str]]
) -> list[str]:
    """taxonomy.yml 규칙이 가리키는 버킷이 분류마스터에 실재하는지 본다.

    매출장에서 분류 이름을 바꾸면 규칙이 유령 버킷을 가리키게 되고, 그러면 과거
    연도만 엉뚱한 항목으로 빠진다. 총액은 맞으니 눈으로는 안 보인다.
    """
    if not master:
        return []
    majors = set(master)
    problems = []
    for view_name, view in views.items():
        if view.get("prefer_raw") is None:
            continue
        targets = set((view.get("base") or {}).values())
        for rule in view.get("exceptions") or []:
            targets.add(rule["then"])
        for target in sorted(targets - majors):
            problems.append(
                f"[{view_name}] 규칙이 «{target}»로 보내는데 분류마스터에 그런 대분류가 없다."
                " 매출장에서 이름이 바뀌었다면 taxonomy.yml 의 base·exceptions 도 같이 고친다"
            )
    return problems


def validate_against_master(
    txs: list[dict[str, Any]], master: dict[str, list[str]]
) -> list[str]:
    """매출장이 스스로 정한 (대분류, 세부분류) 조합을 벗어난 행을 찾는다.

    시트 조건부 서식이 빨갛게 칠하는 것과 같은 검사다. 서식은 사람이 봐야 하지만
    이건 경고로 나간다.
    """
    if not master:
        return []
    bad_major: dict[str, int] = defaultdict(int)
    bad_pair: dict[tuple[str, str], int] = defaultdict(int)
    for tx in txs:
        major, sub = tx["category_raw"], tx.get("subcategory_raw", "")
        if not sub:
            continue  # 세부분류를 안 쓰는 연도다
        if major not in master:
            bad_major[major] += tx["amount"]
        elif sub not in master[major]:
            bad_pair[(major, sub)] += tx["amount"]
    problems = [
        f"매출장 대분류 «{major}» {amount:,}원이 분류마스터에 없다."
        " 시트의 분류마스터 탭과 매출장 I열이 어긋났다"
        for major, amount in bad_major.items()
    ]
    problems += [
        f"«{major}» 아래에 «{sub}»는 분류마스터에 없는 세부분류다 ({amount:,}원)."
        " 매출장 J열을 고치거나 분류마스터에 추가한다"
        for (major, sub), amount in bad_pair.items()
    ]
    return problems
