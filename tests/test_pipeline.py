import json
import zipfile

import pytest

from pulse import analysis, cli, ingest, quality, transform
from tests.conftest import action, synthetic_actions, write_archives


def gold(paths):
    con = transform.build(paths, log=lambda *_: None)
    analysis.build_gold(con)
    return con


def test_full_run_passes_the_gate_and_exports(world):
    assert cli.build(world) == 0
    data = json.loads(world.export.read_text())
    assert data["data_quality"]["passed"]
    assert [y["fiscal_year"] for y in data["years"]] == list(range(2019, 2026))


def test_fiscal_year_starts_on_october_first(world):
    con = gold(world)
    fy = dict(con.execute("SELECT award_key, fiscal_year FROM silver.actions WHERE award_key LIKE 'EDGE%'").fetchall())
    assert fy == {"EDGE1": 2020, "EDGE2": 2019}


def test_net_obligations_include_money_taken_back(world):
    con = gold(world)
    assert con.execute("SELECT obligated, actions FROM silver.awards WHERE award_key = 'GROW1'").fetchone() == (1_900_000, 4)


def test_growth_compares_signed_value_with_latest(world):
    con = gold(world)
    growth, added = con.execute("SELECT growth, added FROM gold.growth WHERE award_key = 'GROW1'").fetchone()
    assert growth == pytest.approx(1.0) and added == 1_000_000


def test_awards_signed_before_the_window_are_not_scored(world):
    con = gold(world)
    assert con.execute("SELECT starts_in_data FROM silver.awards WHERE award_key = 'OLD1'").fetchone() == (False,)
    assert con.execute("SELECT count(*) FROM gold.growth WHERE award_key = 'OLD1'").fetchone() == (0,)


def test_subsidiaries_count_under_their_parent(world):
    con = gold(world)
    top_share, top = con.execute("SELECT top_share, top_supplier FROM gold.concentration WHERE psc = '6505'").fetchone()
    assert top == "ACME HOLDINGS" and top_share == pytest.approx(50 / 55)


def test_competition_separates_sole_source_and_single_bid(world):
    con = gold(world)
    nc, sb = con.execute("SELECT not_competed, single_bid FROM gold.competition WHERE fiscal_year = 2023").fetchone()
    assert nc == 3_000_000 and sb == 5_000_000
    # the last week of September holds the sole-source award and the $100 award on 30 Sep 2019
    obligated, share = con.execute("SELECT obligated, not_competed_share FROM gold.year_end WHERE last_week").fetchone()
    assert obligated == 3_000_100 and share == pytest.approx(3_000_000 / 3_000_100)


def test_veteran_owned_share(world):
    con = gold(world)
    share = con.execute("SELECT sdvosb_share FROM gold.veterans WHERE fiscal_year = 2023").fetchone()[0]
    assert share == pytest.approx(3 / 58)


def test_gate_catches_duplicate_actions(tmp_path, monkeypatch):
    from pulse import config
    monkeypatch.setattr(config, "MIN_CATEGORY_SPEND", 1_000_000)
    rows = synthetic_actions()
    rows.append(dict(rows[0]))          # the same action reported twice
    write_archives(tmp_path, rows)
    con = gold(config.Paths(tmp_path))
    failed = {c.name for c in quality.run(con) if not c.passed}
    assert "one_row_per_action" in failed


def test_missing_columns_fail_loudly(tmp_path):
    from pulse import config
    archives = tmp_path / "data" / "bronze" / "archives"
    archives.mkdir(parents=True)
    with zipfile.ZipFile(archives / "FY2020_036_Contracts_Full_20261106.zip", "w") as z:
        z.writestr("part.csv", "action_date,federal_action_obligation\n2020-01-01,5\n")
    with pytest.raises(RuntimeError, match="missing expected columns"):
        transform.build(config.Paths(tmp_path), log=lambda *_: None)


def test_truncated_download_is_rejected(tmp_path):
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("a.csv", "x\n" * 1000)
    bad = tmp_path / "bad.zip"
    bad.write_bytes(good.read_bytes()[:-40])
    assert ingest.is_valid_zip(good) and not ingest.is_valid_zip(bad)
