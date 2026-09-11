"""
매출장에 없는 사실을 CRM 에서 끌어와 보강 테이블(enrichment.yml)을 만든다.

매출장은 「누구에게 얼마」만 안다. 학교급·교육청·예산출처·사업 발주처·운영기간·
사업비는 모른다. 그건 노션 CRM(Athena 미러 notion_prd)에 있다. 매출장 거래처를
축으로 그 둘을 잇는다.

매일 빌드와 분리한 이유:
  - 빌드는 결정론적이어야 한다. 매일 외부 시스템 상태에 따라 숫자가 흔들리면 안 된다.
  - 보강값은 사람이 검토하고 커밋한다. enrichment.yml 은 diff 로 남는다.

CRM 에도 없는 값은 추측해 채우지 않는다. needs_research 큐에 남기고, 사람이
확인한 것만 manual 아래에 적는다. manual 이 언제나 이긴다.
"""

import asyncio
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from api.redash import create_query_result, get_job, get_query_result
from service.revenue.build import customer_key

KST = timezone(timedelta(hours=9))

# Redash 의 AWS Athena 데이터 소스
DATA_SOURCE_ID = 1
JOB_SUCCESS, JOB_FAILURE = 3, 4

SUFFIX_EXPAND = [
    ("여고", "여자고등학교"),
    ("여중", "여자중학교"),
    ("여초", "여자초등학교"),
    ("고", "고등학교"),
    ("중", "중학교"),
    ("초", "초등학교"),
]
REGION_PREFIX = re.compile(
    r"^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주"
    r"|고창|남원|천안|아산|공주|청주|전주|익산|순천|여수|목포|김해|창원|진주|포항|구미)\s+"
)
LEVEL_SUFFIX = [
    ("초등학교", "초등학교"),
    ("중학교", "중학교"),
    ("고등학교", "고등학교"),
    ("중고등학교", "중학교"),
    ("여자고등학교", "고등학교"),
    ("여자중학교", "중학교"),
    ("여고", "고등학교"),
    ("여중", "중학교"),
    ("고", "고등학교"),
    ("중", "중학교"),
    ("초", "초등학교"),
]
NOT_A_SCHOOL = ("대학교", "산학협력단", "산단", "교육청", "교육원", "연구정보원")
SCHOOL_CATEGORIES = ("초등", "중등", "고등", "짓다")

SQL_ORGS = """
SELECT school_name AS school, max(school_level) AS level, max(edu_office) AS office
FROM notion_prd.organizations
WHERE school_name IS NOT NULL AND school_name <> ''
GROUP BY school_name
"""

SQL_DEALS = """
SELECT o.school_name AS school,
       array_join(array_sort(array_agg(DISTINCT m)), ',')                     AS models,
       array_join(array_sort(array_agg(DISTINCT nullif(d."예산출처",''))), ',') AS budget,
       array_join(array_sort(array_agg(DISTINCT nullif(d."사용학기",''))), ',') AS terms,
       count(DISTINCT d.id) AS deals,
       sum(COALESCE(TRY(CAST(NULLIF(regexp_replace(CAST(d."인원" AS VARCHAR),'[^0-9]',''),'') AS INTEGER)),0)) AS seats
FROM notion_prd.organizations o
JOIN notion_prd.deals d ON contains(d."학교", o.id)
LEFT JOIN UNNEST(COALESCE(d."모델", ARRAY[''])) AS t(m) ON TRUE
GROUP BY o.school_name
HAVING count(DISTINCT d.id) > 0
"""

SQL_PROGRAMS = """
SELECT "이름" AS name, "발주처" AS client, "연도" AS year, "사업비" AS budget,
       "실매출" AS our_revenue, "운영기간" AS period, "사업단계" AS stage,
       "교육규모" AS scale, "자사포지션" AS position
FROM notion_prd.government_contracts
WHERE "이름" IS NOT NULL AND "이름" <> ''
"""

CAVEAT = (
    "notion_prd.organizations 는 10,000행에서 잘려 있다(고등학교 1,909 · 실제 약 2,380). "
    "빠진 학교는 학교급만 이름에서 도출하고 교육청·지역은 비워 둔다."
)
HOW_TO_EDIT = (
    "customers·programs·needs_research 는 자동 갱신되니 손대지 않는다. 고칠 값은 manual 아래에 "
    "같은 거래처 키로 적는다. manual 이 언제나 이긴다. 새 사업은 program_aliases 에 한 줄 더한다."
)


async def run_sql(sql: str, timeout_seconds: int = 300) -> list[dict[str, Any]]:
    """Redash 로 Athena 에 SQL 을 실행하고 행 목록을 돌려준다."""
    response = await create_query_result(sql, DATA_SOURCE_ID, max_age=0)
    if "query_result" in response:
        return response["query_result"]["data"]["rows"]
    job_id = response["job"]["id"]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        job = (await get_job(job_id))["job"]
        if job["status"] == JOB_SUCCESS:
            result = await get_query_result(job["query_result_id"])
            return result["query_result"]["data"]["rows"]
        if job["status"] == JOB_FAILURE:
            raise RuntimeError(f"Redash 쿼리 실패: {job.get('error')}")
        await asyncio.sleep(1.5)
    raise TimeoutError(f"Redash 쿼리가 {timeout_seconds}초 안에 끝나지 않았다")


def school_variants(name: str) -> list[str]:
    """매출장에 적힌 약칭에서 정식 학교명 후보를 만든다.

    「도담고」-> 도담고등학교 · 「고창 영선중」-> 영선중학교 · 「세종 종촌중」-> 종촌중학교
    """
    base = re.sub(r"\s+", " ", name).strip()
    stripped = REGION_PREFIX.sub("", base)
    candidates = [base, base.replace(" ", ""), stripped, stripped.replace(" ", "")]
    if " " in base:
        candidates.append(base.split()[-1])

    out: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in out:
            continue
        out.append(candidate)
        if candidate.endswith("학교"):
            continue
        for short, full in SUFFIX_EXPAND:
            if candidate.endswith(short):
                expanded = candidate[: -len(short)] + full
                if expanded not in out:
                    out.append(expanded)
                break
    return out


def derive_level(name: str) -> str:
    """학교명 접미사에서 학교급을 확정한다. CRM 마스터가 잘려 빠진 학교용."""
    base = re.sub(r"\s+", "", name)
    if any(token in base for token in NOT_A_SCHOOL):
        return ""
    for suffix, level in sorted(LEVEL_SUFFIX, key=lambda pair: -len(pair[0])):
        if base.endswith(suffix):
            return level
    return ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> int:
    try:
        return int(round(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return 0


def ledger_counterparties(
    raw: dict[str, Any], sources: dict[str, Any]
) -> dict[str, dict]:
    """raw 스냅샷에서 거래처별 {names, cats} 를 뽑는다."""
    base_cols = sources["ledger"]["columns"]
    out: dict[str, dict] = {}
    for blob in raw["tabs"].values():
        cols = {**base_cols, **(blob.get("columns") or {})}
        for row in blob["rows"][1:]:
            row = list(row) + [""] * 12
            name = re.sub(
                r"\s+", " ", str(row[cols["counterparty"]]).replace("\t", " ")
            ).strip()
            if not name:
                continue
            record = out.setdefault(customer_key(name), {"names": set(), "cats": set()})
            record["names"].add(name)
            record["cats"].add(str(row[cols["category_raw"]]).strip())
    return out


def build_enrichment(
    ledger: dict[str, dict],
    orgs: list[dict],
    deals: list[dict],
    programs: list[dict],
    existing: dict[str, Any],
) -> dict[str, Any]:
    """CRM 결과를 매출장 거래처에 붙여 enrichment.yml 내용을 만든다. 순수 함수다.

    Args:
        ledger: ledger_counterparties 결과
        orgs: SQL_ORGS 행
        deals: SQL_DEALS 행
        programs: SQL_PROGRAMS 행
        existing: 지금 enrichment.yml. manual 과 program_aliases 를 이어받는다
    """
    by_name: dict[str, list[dict]] = defaultdict(list)
    for row in orgs:
        by_name[re.sub(r"\s+", "", _text(row["school"]))].append(row)
    deals_by_school = {_text(row["school"]): row for row in deals}

    manual = existing.get("manual") or {}
    aliases = existing.get("program_aliases") or {}

    customers: dict[str, dict] = {}
    needs_research: list[dict] = []
    non_school: list[dict] = []
    ambiguous: list[dict] = []

    for key, info in sorted(ledger.items()):
        display = sorted(info["names"], key=len, reverse=True)[0]
        hit = None
        for candidate in school_variants(display):
            rows = by_name.get(re.sub(r"\s+", "", candidate))
            if rows and len(rows) == 1:
                hit = rows[0]
                break
            if rows and len(rows) > 1:
                ambiguous.append({"ledger_name": display, "candidates": len(rows)})
                break
        if not hit:
            level = derive_level(display)
            if level:
                # CRM 마스터에 없는 학교. 학교급은 이름에서 확정할 수 있으므로 그것만 채운다.
                customers[key] = {
                    "name": display,
                    "school_level": level,
                    "level_source": "이름 접미사에서 도출 (CRM 마스터에 없음)",
                }
                needs_research.append(
                    {
                        "ledger_name": display,
                        "derived_level": level,
                        "ledger_categories": sorted(info["cats"]),
                        "needs": "교육청·지역·예산출처",
                    }
                )
            elif any(category in info["cats"] for category in SCHOOL_CATEGORIES):
                # 학교가 아닌데 학교급 분류에 들어가 있다. 채널·리셀러·출판사 경유 건이다.
                non_school.append(
                    {"ledger_name": display, "ledger_categories": sorted(info["cats"])}
                )
            continue

        school = _text(hit["school"])
        deal = deals_by_school.get(school, {})
        record = {
            "name": display,
            "crm_school_name": school if school != display else "",
            "school_level": _text(hit.get("level")),
            "edu_office": _text(hit.get("office")),
            "models": [x for x in _text(deal.get("models")).split(",") if x],
            "budget_source": [x for x in _text(deal.get("budget")).split(",") if x],
            "terms": [x for x in _text(deal.get("terms")).split(",") if x],
            "crm_deals": _number(deal.get("deals")),
            "crm_seats": _number(deal.get("seats")),
        }
        customers[key] = {k: v for k, v in record.items() if v not in ("", [], 0)}

    program_master: dict[str, dict] = {}
    for row in programs:
        name = _text(row.get("name"))
        if not name:
            continue
        record = {
            "name": name,
            "client": _text(row.get("client")),
            "year": _text(row.get("year")),
            "total_budget": _number(row.get("budget")),
            "our_revenue": _number(row.get("our_revenue")),
            "period_start": _text(row.get("period")),
            "stage": _text(row.get("stage")),
            "scale": _number(row.get("scale")),
            "position": _text(row.get("position")),
        }
        program_master[name] = {k: v for k, v in record.items() if v not in ("", 0)}

    dangling = sorted(
        {target for target in aliases.values() if target not in program_master}
    )

    return {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "source": "notion_prd (Athena) via Redash · organizations + deals + government_contracts",
        "how_to_edit": HOW_TO_EDIT,
        "coverage": {
            "ledger_counterparties": len(ledger),
            "matched": len(customers),
            "level_derived_from_name": len(needs_research),
            "non_school_in_school_category": len(non_school),
            "ambiguous": len(ambiguous),
            "alias_targets_missing_from_master": dangling,
            "caveat": CAVEAT,
        },
        "manual": manual,
        "program_aliases": aliases,
        "customers": customers,
        "programs": program_master,
        "needs_research": needs_research,
        "non_school_in_school_category": non_school,
        "ambiguous": ambiguous,
    }
