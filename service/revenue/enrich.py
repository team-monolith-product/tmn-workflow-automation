import asyncio
import json
import re
from typing import Any

from api.redash import create_query_result, get_job, get_query_result
from service.revenue.normalize import clean, number

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


SQL_ORGS = """
SELECT school_name AS school, school_level AS level, edu_office AS office
FROM notion_prd.organizations
WHERE school_name IS NOT NULL AND school_name <> ''
"""
SQL_DEALS = """
SELECT o.school_name AS school,
       json_format(CAST(array_sort(array_agg(DISTINCT nullif(d."예산출처", ''))) AS JSON)) AS budget,
       json_format(CAST(array_sort(array_agg(DISTINCT nullif(d."사용학기", ''))) AS JSON)) AS terms
FROM notion_prd.organizations o
JOIN notion_prd.deals d ON contains(d."학교", o.id)
GROUP BY o.school_name
"""
SQL_PROGRAMS = """
SELECT "이름" AS name, "발주처" AS client, "사업비" AS budget,
       "실매출" AS our_revenue, "사업단계" AS stage
FROM notion_prd.government_contracts
WHERE "이름" IS NOT NULL AND "이름" <> ''
"""


async def run_sql(sql: str, timeout_seconds: int = 300) -> list[dict[str, Any]]:
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
    base = re.sub(r"\s+", "", name)
    if any(token in base for token in NOT_A_SCHOOL):
        return ""
    for suffix, level in sorted(LEVEL_SUFFIX, key=lambda pair: -len(pair[0])):
        if base.endswith(suffix):
            return level
    return ""


def customer_key(name: str) -> str:
    name = re.sub(
        r"주식회사|㈜|\(주\)|\(재\)|재단법인|사단법인|\(사\)|유한회사|\(유\)|법인",
        "",
        name,
    )
    return re.sub(r"\s+", "", name)


def crm_list(value: Any) -> list[str]:
    values = json.loads(value) if isinstance(value, str) else (value or [])
    return [v for v in values if v]


def customer_info(name: str, orgs: list[dict], deals: list[dict], manual: dict) -> dict:
    result = {}
    for candidate in school_variants(name):
        matches = [
            o
            for o in orgs
            if customer_key(clean(o["school"])) == customer_key(candidate)
        ]
        if len(matches) > 1:
            break
        if matches:
            school = matches[0]
            deal = next((d for d in deals if d["school"] == school["school"]), {})
            result = {
                "school_level": clean(school.get("level")) or None,
                "edu_office": clean(school.get("office")) or None,
                "budget_sources": crm_list(deal.get("budget")),
                "terms": crm_list(deal.get("terms")),
            }
            break
    if not result:
        result["school_level"] = derive_level(name) or None
    overrides = manual.get(customer_key(name), {})
    for column in ("school_level", "edu_office", "terms"):
        if column in overrides:
            result[column] = overrides[column]
    if "budget_source" in overrides:
        result["budget_sources"] = overrides["budget_source"]
    return result


def enrich_rows(
    rows: list[dict],
    orgs: list[dict],
    deals: list[dict],
    programs: list[dict],
    rules: dict,
) -> list[str]:
    warnings = []
    aliases = sorted(
        rules.get("program_aliases", {}).items(), key=lambda pair: -len(pair[0])
    )
    for row in rows:
        row.update(customer_info(row["customer"], orgs, deals, rules.get("manual", {})))
        for alias, name in aliases:
            if alias not in f"{row['customer']} {row['item']}":
                continue
            matches = [p for p in programs if clean(p["name"]) == name]
            if len(matches) != 1:
                warnings.append(f"사업 «{name}» CRM 매칭 {len(matches)}건: 보강 생략")
                break
            program = matches[0]
            row.update(
                program_name=name,
                program_client=clean(program.get("client")) or None,
                program_budget=number(program.get("budget")),
                program_our_revenue=number(program.get("our_revenue")),
                program_stage=clean(program.get("stage")) or None,
            )
            break
    return sorted(set(warnings))


async def fetch_crm() -> tuple[list[dict], list[dict], list[dict]]:
    orgs = await run_sql(SQL_ORGS)
    deals = await run_sql(SQL_DEALS)
    programs = await run_sql(SQL_PROGRAMS)
    return orgs, deals, programs
