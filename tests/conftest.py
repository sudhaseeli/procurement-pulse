"""A small synthetic VA archive in the real USAspending format, with known answers built in."""
import csv
import io
import zipfile

import pytest

from pulse import config
from pulse.transform import COLUMNS

FILLER = ["recipient_state_code", "awarding_office_name", "naics_description"]   # extra columns the pipeline ignores


def action(key, award, mod, date, obligation, *, potential, supplier="ACME", parent="ACME HOLDINGS", psc="6505",
           competed="A", offers="3", pricing="J", award_type="C", sdvosb="f", action_type="", uei=None):
    return {
        "contract_transaction_unique_key": key, "contract_award_unique_key": award, "award_id_piid": award,
        "modification_number": mod, "action_date": date, "federal_action_obligation": str(obligation),
        "base_and_all_options_value": str(potential), "base_and_exercised_options_value": str(potential),
        "awarding_sub_agency_name": "Veterans Health Administration",
        "recipient_uei": uei or f"UEI-{supplier}", "recipient_name": supplier,
        "recipient_parent_uei": f"P-{parent}", "recipient_parent_name": parent,
        "award_type_code": award_type, "type_of_contract_pricing_code": pricing,
        "product_or_service_code": psc, "product_or_service_code_description": f"Category {psc}",
        "naics_code": "325412", "extent_competed_code": competed, "number_of_offers_received": offers,
        "type_of_set_aside_code": "NONE", "action_type_code": action_type,
        "service_disabled_veteran_owned_business": sdvosb, "veteran_owned_business": sdvosb,
        "recipient_state_code": "VA", "awarding_office_name": "NAC", "naics_description": "Pharma",
    }


def synthetic_actions():
    rows = []
    # every fiscal year gets steady drug buying, dominated by one parent company (two subsidiaries)
    for fy in config.FISCAL_YEARS:
        rows.append(action(f"T{fy}-1", f"DRUG{fy}", "0", f"{fy - 1}-11-15", 40_000_000, potential=40_000_000,
                           supplier="ACME EAST", parent="ACME HOLDINGS"))
        rows.append(action(f"T{fy}-2", f"DRUGB{fy}", "0", f"{fy}-02-10", 10_000_000, potential=10_000_000,
                           supplier="ACME WEST", parent="ACME HOLDINGS", uei="UEI-ACMEWEST"))
        rows.append(action(f"T{fy}-3", f"RIVAL{fy}", "0", f"{fy}-03-10", 5_000_000, potential=5_000_000,
                           supplier="RIVAL", parent="RIVAL INC", competed="A", offers="1"))
    # an IT services contract that doubles in value through modifications; like FPDS, each action carries the change
    rows += [
        action("G-0", "GROW1", "0", "2021-01-05", 1_000_000, potential=1_000_000, supplier="BYTES", parent="BYTES LLC",
               psc="DA01", pricing="Y"),
        action("G-1", "GROW1", "P00001", "2021-06-01", 600_000, potential=600_000, supplier="BYTES",
               parent="BYTES LLC", psc="DA01", pricing="Y", action_type="B"),
        action("G-2", "GROW1", "P00002", "2022-02-01", 400_000, potential=400_000, supplier="BYTES",
               parent="BYTES LLC", psc="DA01", pricing="Y", action_type="B"),
        action("G-3", "GROW1", "P00003", "2022-08-01", -100_000, potential=0, supplier="BYTES",
               parent="BYTES LLC", psc="DA01", pricing="Y", action_type="M"),
    ]
    # a sole-source award signed in the last week of September, by a service-disabled veteran-owned business
    rows.append(action("Y-0", "YEAREND", "0", "2023-09-28", 3_000_000, potential=3_000_000, supplier="VETCO",
                       parent="VETCO", psc="Q201", competed="C", offers="1", sdvosb="t"))
    # an award signed before the window: only its later modification appears
    rows.append(action("OLD-5", "OLD1", "P00005", "2020-05-01", 2_000_000, potential=9_000_000, supplier="LEGACY",
                       parent="LEGACY CO", psc="Z1AA"))
    # the first day of FY2020 and the last day of FY2019
    rows.append(action("EDGE-1", "EDGE1", "0", "2019-10-01", 100, potential=100, supplier="EDGE", parent="EDGE"))
    rows.append(action("EDGE-2", "EDGE2", "0", "2019-09-30", 100, potential=100, supplier="EDGE", parent="EDGE"))
    return rows


def write_archives(root, rows):
    archives = root / "data" / "bronze" / "archives"
    archives.mkdir(parents=True, exist_ok=True)
    fields = list(COLUMNS) + FILLER
    by_fy = {}
    for r in rows:
        y, m = int(r["action_date"][:4]), int(r["action_date"][5:7])
        by_fy.setdefault(y + (m >= 10), []).append(r)
    for fy, items in by_fy.items():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        w.writerows(items)
        with zipfile.ZipFile(archives / f"FY{fy}_036_Contracts_Full_20261106.zip", "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"FY{fy}_036_Contracts_Full_20261106_1.csv", buf.getvalue())


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MIN_CATEGORY_SPEND", 1_000_000)
    write_archives(tmp_path, synthetic_actions())
    return config.Paths(tmp_path)
