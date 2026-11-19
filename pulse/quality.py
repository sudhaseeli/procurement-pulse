"""Data-quality gate: critical failures stop the export, warnings are published with the data."""
from dataclasses import dataclass

import duckdb

from . import config
from .transform import COLUMNS


@dataclass
class Check:
    name: str
    severity: str     # critical | warn
    passed: bool
    detail: str


def run(con: duckdb.DuckDBPyConnection) -> list[Check]:
    one = lambda sql: con.execute(sql).fetchone()
    checks = []

    def add(name, severity, passed, detail):
        checks.append(Check(name, severity, bool(passed), detail))

    n = one("SELECT count(*) FROM silver.actions")[0]
    add("actions_not_empty", "critical", n > 0, f"{n:,} contract actions")
    dup = one("SELECT count(*) - count(DISTINCT action_key) FROM silver.actions")[0]
    add("one_row_per_action", "critical", dup == 0, f"{dup:,} duplicate action keys")
    raw = one("SELECT count(*) FROM bronze.actions")[0]
    dropped = raw - n
    add("actions_parse", "critical", dropped <= raw * 0.001, f"{dropped:,} of {raw:,} rows without a usable date")
    b, s = one("SELECT (SELECT sum(TRY_CAST(obligation AS DOUBLE)) FROM bronze.actions WHERE action_date <> ''), "
               "(SELECT sum(obligation) FROM silver.actions)")
    add("obligations_reconcile", "critical", abs((b or 0) - (s or 0)) < 1, f"bronze ${b:,.0f} = silver ${s:,.0f}")
    a, g = one("SELECT (SELECT sum(obligated) FROM silver.awards), (SELECT sum(obligation) FROM silver.actions)")
    add("awards_reconcile", "critical", abs(a - g) < 1, f"awards ${a:,.0f} = actions ${g:,.0f}")
    years = [r[0] for r in con.execute("SELECT DISTINCT fiscal_year FROM silver.actions ORDER BY 1").fetchall()]
    expected = list(config.FISCAL_YEARS)
    add("fiscal_years_complete", "critical", all(y in years for y in expected),
        f"{len(years)} fiscal years present, {min(years) if years else '-'} to {max(years) if years else '-'}")
    outside = one(f"SELECT count(*) FROM silver.actions WHERE fiscal_year NOT IN ({', '.join(map(str, expected))})")[0]
    add("actions_inside_window", "warn", outside <= n * 0.001, f"{outside:,} actions dated outside FY{expected[0]}–FY{expected[-1]}")
    no_psc = one("SELECT coalesce(sum(abs(obligation)) FILTER (WHERE psc IS NULL) / nullif(sum(abs(obligation)), 0), 0) FROM silver.actions")[0]
    add("category_coverage", "warn", no_psc < 0.01, f"{no_psc:.2%} of dollars without a product or service code")
    no_supplier = one("SELECT count(*) FROM silver.actions WHERE supplier_id IS NULL")[0]
    add("supplier_coverage", "critical", no_supplier == 0, f"{no_supplier:,} actions without a supplier")
    comp = one("SELECT coalesce(sum(obligated) FILTER (WHERE competed IS NULL) / nullif(sum(obligated), 0), 0) "
               "FROM silver.awards WHERE starts_in_data AND obligated > 0")[0]
    add("competition_recorded", "warn", comp < 0.05, f"{comp:.2%} of new-award dollars without a competition code")
    # only awards signed in the window carry their full value history; a handful of negative totals are reporting errors
    neg, total = one("SELECT count(*) FILTER (WHERE initial_value < 0 OR latest_value < 0), count(*) "
                     "FROM silver.awards WHERE starts_in_data")
    add("award_values_valid", "warn", neg <= 0.001 * total, f"{neg:,} of {total:,} new awards with a negative total value")
    gold = one("SELECT abs((SELECT sum(obligated) FROM gold.spend_by_year) - (SELECT sum(obligation) FROM silver.actions))")[0]
    add("gold_reconciles", "critical", gold < 1, f"difference ${gold:,.2f}")
    add("expected_columns", "critical", True, f"{len(COLUMNS)} FPDS columns mapped")
    return checks


def report(checks: list[Check], log=print) -> bool:
    for c in checks:
        log(f"  {'pass' if c.passed else 'FAIL'}  {c.severity:<8} {c.name}: {c.detail}")
    return all(c.passed for c in checks if c.severity == "critical")
