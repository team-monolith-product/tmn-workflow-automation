from psycopg import sql

from service.db import connect, fetch_all, fetch_one

DIMENSIONS = {
    "category": "상품 분류",
    "subcategory": "세부분류",
    "school_level": "학교급",
    "edu_office": "교육청",
    "budget_sources": "예산출처",
}
PAGE_SIZE = 100


def group_totals(
    conn, year: int | None, column: str, limit: int | None = None
) -> list[dict]:
    source = sql.SQL("revenue_transactions")
    label = sql.Identifier(column)
    if column == "budget_sources":
        source += sql.SQL(
            " LEFT JOIN LATERAL (SELECT DISTINCT unnest(budget_sources) AS budget) tags ON true"
        )
        label = sql.SQL("coalesce(budget, '미기재')")
    elif column != "customer":
        label = sql.SQL("coalesce(nullif({}, ''), '미기재')").format(label)
    return fetch_all(
        conn,
        sql.SQL(
            """
        SELECT {} AS label, sum(amount) AS amount FROM {}
        WHERE year=%s AND status='issued'
        GROUP BY label ORDER BY amount DESC LIMIT %s
    """
        ).format(label, source),
        (year, limit),
    )


def dashboard_data(
    year: int | None = None,
    dimension: str = "category",
    search: str = "",
    page: int = 1,
    status: str = "issued",
) -> dict:
    if dimension not in DIMENSIONS or status not in ("issued", "planned"):
        raise ValueError("지원하지 않는 조회 조건입니다")
    with connect(read_only=True) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        yearly = fetch_all(
            conn,
            """
            SELECT year, coalesce(sum(amount) FILTER (WHERE status='issued'), 0) AS issued,
                   coalesce(sum(amount) FILTER (WHERE status='planned'), 0) AS planned,
                   count(*) FILTER (WHERE status='issued') AS count,
                   count(DISTINCT customer) FILTER (WHERE status='issued') AS customers
            FROM revenue_transactions GROUP BY year ORDER BY year DESC
        """,
        )
        if year is None:
            year = yearly[0]["year"] if yearly else None
        totals = {
            key: next((row[key] for row in yearly if row["year"] == year), 0)
            for key in ("issued", "planned", "count", "customers")
        }
        yearly = [
            {key: row[key] for key in ("year", "issued", "planned")} for row in yearly
        ]
        monthly = fetch_all(
            conn,
            """
            SELECT extract(month FROM issued_on)::integer AS month, sum(amount) AS amount
            FROM revenue_transactions WHERE year=%s AND status='issued'
            GROUP BY month ORDER BY month
        """,
            (year,),
        )
        groups = group_totals(conn, year, dimension)
        customers = group_totals(conn, year, "customer", limit=20)
        where = "year=%s AND status=%s AND strpos(lower(concat_ws(' ', customer, item, note)), lower(%s)) > 0"
        params = (year, status, search)
        count = fetch_one(
            conn,
            f"SELECT count(*) AS count FROM revenue_transactions WHERE {where}",
            params,
        )["count"]
        pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(1, page), pages)
        rows = fetch_all(
            conn,
            f"""
            SELECT * FROM revenue_transactions WHERE {where}
            ORDER BY amount DESC, issued_on NULLS LAST, customer, item, category,
                     subcategory NULLS LAST, note NULLS LAST, quantity NULLS LAST,
                     unit_price NULLS LAST, tax NULLS LAST, total NULLS LAST
            LIMIT %s OFFSET %s
        """,
            params + (PAGE_SIZE, (page - 1) * PAGE_SIZE),
        )
    return {
        "year": year,
        "yearly": yearly,
        "totals": totals,
        "monthly": monthly,
        "groups": groups,
        "customers": customers,
        "rows": rows,
        "count": count,
        "page": page,
        "pages": pages,
        "dimension": dimension,
        "dimensions": DIMENSIONS,
        "search": search,
        "status": status,
    }
