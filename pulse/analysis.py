"""Gold: the procurement questions, answered from silver.

Spend is net obligations (money committed, minus money taken back). Suppliers are grouped by their
parent company so that subsidiaries count together. Competition and bid counts come from each award's
first action, because that is when the buying decision was made.
"""
import duckdb

from . import config

NOT_COMPETED = ", ".join(f"'{c}'" for c in config.NOT_COMPETED)
COMPETED = ", ".join(f"'{c}'" for c in config.COMPETED)
PRICING = {"J": "Firm fixed price", "K": "Fixed price with economic price adjustment",
           "L": "Fixed price incentive", "M": "Fixed price award fee", "R": "Cost plus award fee",
           "S": "Cost no fee", "T": "Cost sharing", "U": "Cost plus fixed fee", "V": "Cost plus incentive fee",
           "Y": "Time and materials", "Z": "Labor hours", "1": "Order dependent", "2": "Combination", "3": "Other"}


def build_spend(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE OR REPLACE TABLE gold.spend_by_year AS
        SELECT fiscal_year, sum(obligation) AS obligated, count(*) AS actions,
               count(DISTINCT award_key) AS awards, count(DISTINCT supplier_id) AS suppliers
        FROM silver.actions GROUP BY ALL ORDER BY fiscal_year
    """)
    con.execute("""
        CREATE OR REPLACE TABLE gold.spend_by_group AS
        SELECT c.group_code, c.group_name, c.is_product, a.fiscal_year, sum(a.obligation) AS obligated
        FROM silver.actions a JOIN silver.categories c USING (psc) GROUP BY ALL
    """)
    con.execute("""
        CREATE OR REPLACE TABLE gold.suppliers AS
        SELECT supplier_id, any_value(supplier_name ORDER BY action_date DESC) AS supplier_name,
               sum(obligation) AS obligated, count(DISTINCT award_key) AS awards, count(DISTINCT psc) AS categories,
               bool_or(sdvosb) AS sdvosb
        FROM silver.actions GROUP BY supplier_id
    """)
    # Pareto: how few suppliers receive most of the money
    con.execute("""
        CREATE OR REPLACE TABLE gold.pareto AS
        WITH s AS (SELECT obligated, row_number() OVER (ORDER BY obligated DESC) AS rank,
                          sum(obligated) OVER (ORDER BY obligated DESC ROWS UNBOUNDED PRECEDING) AS running,
                          sum(obligated) OVER () AS total, count(*) OVER () AS n
                   FROM gold.suppliers WHERE obligated > 0)
        SELECT rank, n, running / total AS share FROM s
    """)


def build_concentration(con: duckdb.DuckDBPyConnection) -> None:
    """Herfindahl-Hirschman index and top-supplier share per product or service code."""
    con.execute(f"""
        CREATE OR REPLACE TABLE gold.concentration AS
        WITH s AS (SELECT psc, supplier_id, sum(obligation) AS v FROM silver.actions WHERE psc IS NOT NULL GROUP BY ALL HAVING v > 0),
             t AS (SELECT psc, sum(v) AS total, count(*) AS suppliers FROM s GROUP BY psc),
             r AS (SELECT s.*, t.total, t.suppliers, s.v / t.total AS share,
                          row_number() OVER (PARTITION BY s.psc ORDER BY s.v DESC) AS rank FROM s JOIN t USING (psc))
        SELECT r.psc, c.psc_name, c.group_name, any_value(r.total) AS obligated, any_value(r.suppliers) AS suppliers,
               round(10000 * sum(r.share * r.share)) AS hhi,
               max(r.share) AS top_share,
               any_value(r.supplier_id) FILTER (WHERE r.rank = 1) AS top_supplier_id
        FROM r JOIN silver.categories c USING (psc)
        GROUP BY r.psc, c.psc_name, c.group_name
        HAVING any_value(r.total) >= {config.MIN_CATEGORY_SPEND}
    """)
    con.execute("""
        CREATE OR REPLACE TABLE gold.concentration AS
        SELECT g.*, s.supplier_name AS top_supplier FROM gold.concentration g
        LEFT JOIN gold.suppliers s ON s.supplier_id = g.top_supplier_id
    """)


def build_competition(con: duckdb.DuckDBPyConnection) -> None:
    """New awards made inside the window, by how they were competed and how many bids came in."""
    con.execute(f"""
        CREATE OR REPLACE TABLE gold.competition AS
        SELECT start_fy AS fiscal_year,
               sum(obligated) AS obligated,
               coalesce(sum(obligated) FILTER (WHERE competed IN ({NOT_COMPETED})), 0) AS not_competed,
               coalesce(sum(obligated) FILTER (WHERE competed IN ({COMPETED})), 0) AS competed,
               coalesce(sum(obligated) FILTER (WHERE competed IN ({COMPETED}) AND offers = 1), 0) AS single_bid,
               coalesce(sum(obligated) FILTER (WHERE competed IN ({COMPETED}) AND offers IS NOT NULL), 0) AS competed_with_offers,
               count(*) AS awards
        FROM silver.awards WHERE starts_in_data AND obligated > 0
        GROUP BY ALL ORDER BY fiscal_year
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE gold.competition_by_group AS
        SELECT c.group_name, sum(a.obligated) AS obligated,
               coalesce(sum(a.obligated) FILTER (WHERE a.competed IN ({NOT_COMPETED})), 0) / sum(a.obligated) AS not_competed_share,
               coalesce(sum(a.obligated) FILTER (WHERE a.competed IN ({COMPETED}) AND a.offers = 1), 0)
                 / nullif(coalesce(sum(a.obligated) FILTER (WHERE a.competed IN ({COMPETED}) AND a.offers IS NOT NULL), 0), 0) AS single_bid_share
        FROM silver.awards a JOIN silver.categories c USING (psc)
        WHERE a.starts_in_data AND a.obligated > 0 GROUP BY ALL
    """)


def build_growth(con: duckdb.DuckDBPyConnection) -> None:
    """How far each award's total value (base plus all options) moved after it was signed."""
    cases = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in PRICING.items())
    con.execute(f"""
        CREATE OR REPLACE TABLE gold.growth AS
        SELECT award_key, piid, supplier_name, psc, pricing, CASE pricing {cases} ELSE 'Not recorded' END AS pricing_name,
               competed, initial_value, latest_value, latest_value / initial_value - 1 AS growth,
               latest_value - initial_value AS added, actions, start_fy
        FROM silver.awards
        WHERE starts_in_data AND initial_value >= {config.MIN_GROWTH_BASE} AND latest_value IS NOT NULL
          AND award_type IN ('C', 'D')     -- definitive contracts and purchase orders carry their own value
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE gold.growth_by_pricing AS
        SELECT pricing_name, count(*) AS awards, median(growth) AS median_growth,
               avg((growth > {config.GROWTH_FLAG})::INT) AS share_over_flag, sum(greatest(added, 0)) AS added
        FROM gold.growth GROUP BY pricing_name HAVING count(*) >= 30 ORDER BY share_over_flag DESC
    """)


def build_year_end(con: duckdb.DuckDBPyConnection) -> None:
    """Spending by month of the fiscal year, and whether money awarded in September is competed as often."""
    con.execute("""
        CREATE OR REPLACE TABLE gold.by_month AS
        SELECT fiscal_year, (month(action_date) + 2) % 12 + 1 AS fy_month, month(action_date) AS month,
               sum(obligation) AS obligated, count(*) AS actions
        FROM silver.actions GROUP BY ALL
    """)
    con.execute("""
        CREATE OR REPLACE TABLE gold.by_week AS
        SELECT fiscal_year, least(52, (action_date - make_date(fiscal_year - 1, 10, 1)) // 7 + 1) AS fy_week,
               sum(obligation) AS obligated
        FROM silver.actions GROUP BY ALL
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE gold.year_end AS
        SELECT month(first_action) = 9 AS september,
               (month(first_action) = 9 AND day(first_action) >= 24) AS last_week,
               count(*) AS awards, sum(obligated) AS obligated,
               coalesce(sum(obligated) FILTER (WHERE competed IN ({NOT_COMPETED})), 0) / sum(obligated) AS not_competed_share,
               coalesce(sum(obligated) FILTER (WHERE competed IN ({COMPETED}) AND offers = 1), 0)
                 / nullif(coalesce(sum(obligated) FILTER (WHERE competed IN ({COMPETED}) AND offers IS NOT NULL), 0), 0) AS single_bid_share
        FROM silver.awards WHERE starts_in_data AND obligated > 0 GROUP BY ALL
    """)


def build_veterans(con: duckdb.DuckDBPyConnection) -> None:
    """VA's Veterans First program gives priority to veteran-owned small businesses."""
    con.execute("""
        CREATE OR REPLACE TABLE gold.veterans AS
        SELECT fiscal_year, sum(obligation) AS obligated,
               coalesce(sum(obligation) FILTER (WHERE sdvosb), 0) / sum(obligation) AS sdvosb_share,
               coalesce(sum(obligation) FILTER (WHERE vosb OR sdvosb), 0) / sum(obligation) AS vosb_share
        FROM silver.actions GROUP BY ALL ORDER BY fiscal_year
    """)


def build_gold(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")
    for step in (build_spend, build_concentration, build_competition, build_growth, build_year_end, build_veterans):
        step(con)
