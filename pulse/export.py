"""Gold to docs/data/pulse.json, the only file the dashboard reads."""
import json
from datetime import datetime, timezone

import duckdb

from . import config


def rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        out.append({n: (str(v) if hasattr(v, "isoformat") else round(v, 6) if isinstance(v, float) else v)
                    for n, v in zip(names, row)})
    return out


def export(con: duckdb.DuckDBPyConnection, paths: config.Paths, checks) -> dict:
    one = lambda sql: rows(con, sql)[0]
    pareto = {f"p{int(p * 100)}": one(f"SELECT min(rank) AS suppliers FROM gold.pareto WHERE share >= {p}")["suppliers"]
              for p in (0.5, 0.8, 0.9)}
    data = {
        "meta": {
            "agency": config.AGENCY_NAME,
            "first_fy": config.FISCAL_YEARS[0], "last_fy": config.FISCAL_YEARS[-1],
            **one("""SELECT sum(obligation) AS obligated, count(*) AS actions, count(DISTINCT award_key) AS awards,
                            count(DISTINCT supplier_id) AS suppliers, count(DISTINCT psc) AS categories,
                            max(action_date) AS last_action FROM silver.actions"""),
            "suppliers_paid": one("SELECT count(*) AS n FROM gold.suppliers WHERE obligated > 0")["n"],
            "pareto": pareto,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "years": rows(con, "SELECT * FROM gold.spend_by_year ORDER BY fiscal_year"),
        "groups": rows(con, """SELECT group_code, group_name, is_product, sum(obligated) AS obligated,
                                      list(struct_pack(fy := fiscal_year, v := obligated) ORDER BY fiscal_year) AS by_year
                               FROM gold.spend_by_group GROUP BY ALL ORDER BY obligated DESC LIMIT 14"""),
        "suppliers": rows(con, """SELECT supplier_name, obligated, awards, categories, sdvosb
                                  FROM gold.suppliers ORDER BY obligated DESC LIMIT 25"""),
        "pareto_curve": rows(con, """SELECT rank, share FROM gold.pareto
                                     WHERE rank <= 50 OR rank % greatest(1, n // 400) = 0 OR rank = n ORDER BY rank"""),
        "concentration": rows(con, """SELECT psc, psc_name, group_name, obligated, suppliers, hhi, top_share, top_supplier
                                      FROM gold.concentration ORDER BY obligated DESC"""),
        "competition": rows(con, "SELECT * FROM gold.competition ORDER BY fiscal_year"),
        "competition_by_group": rows(con, """SELECT * FROM gold.competition_by_group WHERE obligated > 0
                                             ORDER BY obligated DESC LIMIT 14"""),
        "growth_by_pricing": rows(con, "SELECT * FROM gold.growth_by_pricing"),
        "growth_summary": one(f"""SELECT count(*) AS awards, median(growth) AS median_growth,
                                         avg((growth > {config.GROWTH_FLAG})::INT) AS share_over_flag,
                                         avg((growth < 0)::INT) AS share_shrank,
                                         sum(greatest(added, 0)) AS added, sum(initial_value) AS initial
                                  FROM gold.growth"""),
        "growth_hist": rows(con, """SELECT CASE WHEN growth < -0.25 THEN 'Shrank 25%+' WHEN growth < -0.001 THEN 'Shrank'
                                                WHEN growth <= 0.001 THEN 'No change' WHEN growth <= 0.25 THEN 'Up to +25%'
                                                WHEN growth <= 0.5 THEN '+25–50%' WHEN growth <= 1 THEN '+50–100%'
                                                ELSE 'More than doubled' END AS bucket,
                                           count(*) AS awards, sum(added) AS added
                                    FROM gold.growth GROUP BY ALL"""),
        "growth_top": rows(con, """SELECT piid, supplier_name, pricing_name, initial_value, latest_value, growth, added, start_fy
                                   FROM gold.growth ORDER BY added DESC LIMIT 15"""),
        "by_month": rows(con, """SELECT fy_month, month, avg(obligated) AS avg_obligated,
                                        avg(obligated / t.total) AS avg_share
                                 FROM gold.by_month JOIN (SELECT fiscal_year, sum(obligated) AS total FROM gold.by_month GROUP BY 1) t
                                 USING (fiscal_year) GROUP BY ALL ORDER BY fy_month"""),
        "by_week": rows(con, "SELECT fiscal_year, fy_week, obligated FROM gold.by_week ORDER BY fiscal_year, fy_week"),
        "year_end": rows(con, "SELECT * FROM gold.year_end ORDER BY september, last_week"),
        "veterans": rows(con, "SELECT * FROM gold.veterans ORDER BY fiscal_year"),
        "params": {"growth_flag": config.GROWTH_FLAG, "min_growth_base": config.MIN_GROWTH_BASE,
                   "dependence_share": config.DEPENDENCE_SHARE, "min_category_spend": config.MIN_CATEGORY_SPEND},
        "data_quality": {"passed": all(c.passed for c in checks if c.severity == "critical"),
                         "checks": [c.__dict__ for c in checks]},
    }
    paths.export.parent.mkdir(parents=True, exist_ok=True)
    paths.export.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return data
