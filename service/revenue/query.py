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
        sql.SQL("""
        SELECT {} AS label, sum(amount) AS amount FROM {}
        WHERE year=%s AND status='issued'
        GROUP BY label ORDER BY amount DESC LIMIT %s
    """).format(label, source),
        (year, limit),
    )


def dashboard_data(
    year: int | None = None,
    dimension: str = "category",
    search: str = "",
    page: int = 1,
    status: str = "issued",
    bucket: str | None = None,
    subcategory: str | None = None,
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
                   count(*) FILTER (WHERE status='planned') AS planned_count,
                   count(*) FILTER (WHERE status='issued') AS count,
                   count(DISTINCT customer) FILTER (WHERE status='issued') AS customers
            FROM revenue_transactions GROUP BY year ORDER BY year DESC
        """,
        )
        if year is None:
            year = yearly[0]["year"] if yearly else None
        totals = {
            key: next((row[key] for row in yearly if row["year"] == year), 0)
            for key in ("issued", "planned", "count", "customers", "planned_count")
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
        if bucket is not None:
            if dimension == "budget_sources":
                where += " AND (coalesce(%s = ANY(budget_sources), false) OR (%s = '미기재' AND coalesce(cardinality(budget_sources), 0) = 0))"
                params += (bucket, bucket)
            else:
                where += (
                    sql.SQL(" AND coalesce(nullif({}, ''), '미기재')=%s")
                    .format(sql.Identifier(dimension))
                    .as_string(conn)
                )
                params += (bucket,)
        if subcategory is not None:
            where += " AND coalesce(nullif(subcategory, ''), '미기재')=%s"
            params += (subcategory,)
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
        charts = chart_data(conn, year, dimension, search)
    return {
        "charts": charts,
        "bucket": bucket,
        "subcategory": subcategory,
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


def chart_data(conn, year: int | None, dimension: str, search: str) -> dict:
    monthly = fetch_all(
        conn,
        """
        SELECT year, extract(month FROM issued_on)::integer AS month,
               sum(amount) AS amount, count(*) AS count
        FROM revenue_transactions WHERE status='issued'
        GROUP BY year, month ORDER BY year, month
        """,
    )
    source = sql.SQL("revenue_transactions")
    label = sql.SQL("coalesce(nullif({}, ''), '미기재')").format(
        sql.Identifier(dimension)
    )
    if dimension == "budget_sources":
        source += sql.SQL(
            " LEFT JOIN LATERAL (SELECT DISTINCT unnest(budget_sources) AS budget) tags ON true"
        )
        label = sql.SQL("coalesce(budget, '미기재')")
    groups = fetch_all(
        conn,
        sql.SQL("""
        SELECT year, {} AS label, sum(amount) AS amount, count(*) AS count,
               count(*) FILTER (WHERE strpos(lower(concat_ws(' ', customer, item, note)), lower(%s)) > 0) AS matched
        FROM {} WHERE status='issued'
        GROUP BY year, label ORDER BY year, amount DESC, label
        """).format(label, source),
        (search,),
    )
    subgroups = (
        fetch_all(
            conn,
            """
        SELECT coalesce(nullif(category, ''), '미기재') AS category,
               coalesce(nullif(subcategory, ''), '미기재') AS label,
               sum(amount) AS amount, count(*) AS count
        FROM revenue_transactions WHERE year=%s AND status='issued'
          AND strpos(lower(concat_ws(' ', customer, item, note)), lower(%s)) > 0
        GROUP BY 1, 2 ORDER BY amount DESC, label
        """,
            (year, search),
        )
        if dimension == "category"
        else []
    )
    customers = fetch_all(
        conn,
        """
        SELECT customer AS name, sum(amount) AS amount, count(*) AS count
        FROM revenue_transactions WHERE year=%s AND status='issued'
        GROUP BY customer ORDER BY amount DESC, customer
        """,
        (year,),
    )
    sizes = fetch_all(
        conn,
        """
        SELECT CASE WHEN amount < 0 THEN 0 WHEN amount < 1000000 THEN 1
                    WHEN amount < 5000000 THEN 2 WHEN amount < 10000000 THEN 3
                    WHEN amount < 50000000 THEN 4 WHEN amount < 100000000 THEN 5
                    ELSE 6 END AS bin,
               count(*) AS count, sum(amount) AS amount
        FROM revenue_transactions WHERE year=%s AND status='issued'
        GROUP BY bin ORDER BY bin
        """,
        (year,),
    )
    size_stats = fetch_one(
        conn,
        """
        SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY amount) AS median,
               avg(amount) AS mean, max(amount) AS maximum
        FROM revenue_transactions WHERE year=%s AND status='issued'
        """,
        (year,),
    )
    return {
        "monthly": monthly,
        "groups": groups,
        "subgroups": subgroups,
        "customers": customers,
        "sizes": sizes,
        "size_stats": size_stats,
    }
