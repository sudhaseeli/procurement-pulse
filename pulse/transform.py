"""Silver: contract actions and awards. Gold is built in analysis.py.

Each yearly archive lists every contract action: the first award (modification 0) and every later
modification, which can add money, exercise options, change scope or take money back. Silver keeps
one row per action, then rolls them up into one row per award, recording both the value the award
started with and the value it ended up with.
"""
import shutil
import time
import zipfile

import duckdb

from . import config

# FPDS column -> silver name. Every one must exist in the archives.
COLUMNS = {
    "contract_transaction_unique_key": "action_key",
    "contract_award_unique_key": "award_key",
    "award_id_piid": "piid",
    "modification_number": "mod",
    "action_date": "action_date",
    "federal_action_obligation": "obligation",
    "base_and_all_options_value": "potential_value",
    "base_and_exercised_options_value": "exercised_value",
    "awarding_sub_agency_name": "sub_agency",
    "recipient_uei": "recipient_uei",
    "recipient_name": "recipient_name",
    "recipient_parent_uei": "parent_uei",
    "recipient_parent_name": "parent_name",
    "award_type_code": "award_type",
    "type_of_contract_pricing_code": "pricing",
    "product_or_service_code": "psc",
    "product_or_service_code_description": "psc_name",
    "naics_code": "naics",
    "extent_competed_code": "competed",
    "number_of_offers_received": "offers",
    "type_of_set_aside_code": "set_aside",
    "action_type_code": "action_type",
    "service_disabled_veteran_owned_business": "sdvosb",
    "veteran_owned_business": "vosb",
}

# Top-level service categories are the first letter of a service PSC; products use 2-digit FSC groups.
SERVICE_GROUPS = {
    "A": "Research and development", "B": "Special studies and analyses", "C": "Architect and engineering",
    "D": "IT and telecom services", "E": "Purchase of structures", "F": "Natural resources management",
    "G": "Social services", "H": "Quality control and inspection", "J": "Maintenance and repair of equipment",
    "K": "Modification of equipment", "L": "Technical representative", "M": "Operation of facilities",
    "N": "Installation of equipment", "P": "Salvage services", "Q": "Medical services",
    "R": "Professional and support services", "S": "Utilities and housekeeping", "T": "Photo, map, print, publishing",
    "U": "Education and training", "V": "Transportation, travel, relocation", "W": "Lease or rental of equipment",
    "X": "Lease or rental of facilities", "Y": "Construction of structures", "Z": "Maintenance of real property",
}
PRODUCT_GROUPS = {
    "65": "Medical, dental and veterinary equipment and supplies", "66": "Instruments and laboratory equipment",
    "70": "IT equipment and software", "71": "Furniture", "72": "Household and commercial furnishings",
    "74": "Office machines", "75": "Office supplies", "58": "Communication equipment", "59": "Electrical components",
    "61": "Electric wire, power and distribution equipment", "41": "Refrigeration and air conditioning equipment",
    "42": "Firefighting, rescue and safety equipment", "23": "Motor vehicles", "25": "Vehicle components",
    "36": "Special industry machinery", "45": "Plumbing, heating and waste disposal equipment",
    "56": "Construction and building materials", "62": "Lighting fixtures and lamps", "67": "Photographic equipment",
    "68": "Chemicals", "69": "Training aids and devices", "73": "Food preparation and serving equipment",
    "79": "Cleaning equipment and supplies", "84": "Clothing and individual equipment", "85": "Toiletries",
    "89": "Subsistence (food)", "91": "Fuels and lubricants", "99": "Miscellaneous", "R4": "Professional services",
}


def extract(paths: config.Paths, log=print) -> list[str]:
    """Unzip each archive's CSV parts once; a finished year leaves a marker so re-runs skip it."""
    out = paths.bronze / "csv"
    files = []
    for archive in sorted(paths.archives.glob("FY*_Contracts_Full_*.zip")):
        target = out / archive.stem
        marker = target / "_done"
        if not marker.exists():
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True)
            with zipfile.ZipFile(archive) as z:
                z.extractall(target, [n for n in z.namelist() if n.endswith(".csv")])
            marker.write_text(str(time.time()))
        files += [p.as_posix() for p in sorted(target.rglob("*.csv"))]
    if not files:
        raise RuntimeError("no archives found; run `pulse ingest` first")
    return files


def build_silver(con: duckdb.DuckDBPyConnection, files: list[str], log=print) -> None:
    started = time.time()
    file_list = "[" + ", ".join(f"'{f}'" for f in files) + "]"
    con.execute(f"""CREATE OR REPLACE TEMP VIEW raw AS SELECT * FROM read_csv({file_list}, all_varchar=true,
                    union_by_name=true, header=true, filename=true, max_line_size=10000000)""")
    have = {r[0] for r in con.execute("DESCRIBE raw").fetchall()}
    missing = sorted(set(COLUMNS) - have)
    if missing:
        raise RuntimeError(f"archives are missing expected columns: {missing}")
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")
    select = ", ".join(f'"{src}" AS {dst}' for src, dst in COLUMNS.items())
    con.execute(f"CREATE OR REPLACE TABLE bronze.actions AS SELECT {select}, filename AS source_file FROM raw")

    con.execute("""
        CREATE OR REPLACE TABLE silver.actions AS
        SELECT action_key, award_key, piid, coalesce(nullif(mod, ''), '0') AS mod,
               CAST(action_date AS DATE) AS action_date,
               -- federal fiscal years start on 1 October
               year(CAST(action_date AS DATE)) + (month(CAST(action_date AS DATE)) >= 10)::INT AS fiscal_year,
               coalesce(TRY_CAST(obligation AS DOUBLE), 0) AS obligation,
               TRY_CAST(potential_value AS DOUBLE) AS potential_value,
               TRY_CAST(exercised_value AS DOUBLE) AS exercised_value,
               sub_agency,
               nullif(recipient_uei, '') AS recipient_uei, recipient_name,
               -- a parent can report under more than one UEI over the years, so the parent's name is the key
               coalesce(nullif(trim(regexp_replace(upper(coalesce(nullif(parent_name, ''), recipient_name)), '[^A-Z0-9]+', ' ', 'g')), ''),
                        nullif(parent_uei, ''), recipient_uei) AS supplier_id,
               coalesce(nullif(parent_name, ''), recipient_name) AS supplier_name,
               award_type, nullif(pricing, '') AS pricing,
               nullif(upper(psc), '') AS psc, psc_name, naics,
               nullif(competed, '') AS competed, TRY_CAST(offers AS INT) AS offers,
               nullif(set_aside, '') AS set_aside, nullif(action_type, '') AS action_type,
               sdvosb IN ('t', 'true', 'TRUE', 'Y') AS sdvosb, vosb IN ('t', 'true', 'TRUE', 'Y') AS vosb,
               source_file
        FROM bronze.actions
        WHERE action_date IS NOT NULL AND action_date <> ''
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE silver.categories AS
        SELECT psc, any_value(psc_name ORDER BY action_date DESC) AS psc_name,
               CASE WHEN regexp_matches(psc, '^[0-9]') THEN left(psc, 2) ELSE left(psc, 1) END AS group_code,
               regexp_matches(psc, '^[0-9]') AS is_product
        FROM silver.actions WHERE psc IS NOT NULL GROUP BY psc
    """)
    groups = [(k, v) for k, v in {**SERVICE_GROUPS, **PRODUCT_GROUPS}.items()]
    con.execute("CREATE OR REPLACE TEMP TABLE group_names (group_code VARCHAR, group_name VARCHAR)")
    con.executemany("INSERT INTO group_names VALUES (?, ?)", groups)
    con.execute("""
        CREATE OR REPLACE TABLE silver.categories AS
        SELECT c.*, coalesce(g.group_name, CASE WHEN c.is_product THEN 'Other products (FSC ' || c.group_code || ')'
                                                ELSE 'Other services (' || c.group_code || ')' END) AS group_name
        FROM silver.categories c LEFT JOIN group_names g USING (group_code)
    """)

    # one row per award: how it started (its first action in the data) and where it ended up
    con.execute("""
        CREATE OR REPLACE TABLE silver.awards AS
        WITH ordered AS (
            SELECT *, row_number() OVER (PARTITION BY award_key ORDER BY action_date, TRY_CAST(mod AS INT) NULLS LAST, mod) AS first_rank,
                      row_number() OVER (PARTITION BY award_key ORDER BY action_date DESC, action_key DESC) AS last_rank
            FROM silver.actions
        )
        SELECT f.award_key, f.piid, f.award_type, f.pricing, f.psc, f.naics, f.sub_agency,
               f.action_date AS first_action, f.fiscal_year AS start_fy,
               f.mod = '0' AS starts_in_data,                 -- awarded inside the window, not earlier
               f.competed, f.offers, f.set_aside,
               f.potential_value AS initial_value,
               s.potential_value AS latest_value,              -- each action carries the change, not the running total
               l.action_date AS last_action,
               l.supplier_id, l.supplier_name, l.recipient_name, l.sdvosb, l.vosb,
               s.obligated, s.actions
        FROM ordered f
        JOIN ordered l ON l.award_key = f.award_key AND l.last_rank = 1
        JOIN (SELECT award_key, sum(obligation) AS obligated, sum(potential_value) AS potential_value, count(*) AS actions
              FROM silver.actions GROUP BY award_key) s
          ON s.award_key = f.award_key
        WHERE f.first_rank = 1
    """)
    n = con.execute("SELECT count(*) FROM silver.actions").fetchone()[0]
    a = con.execute("SELECT count(*) FROM silver.awards").fetchone()[0]
    log(f"silver: {n:,} contract actions, {a:,} awards in {time.time() - started:.0f}s")


def build(paths: config.Paths, log=print) -> duckdb.DuckDBPyConnection:
    files = extract(paths, log)
    paths.warehouse.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(paths.warehouse))
    build_silver(con, files, log)
    return con
