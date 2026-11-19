"""Sources, paths and analysis settings."""
from dataclasses import dataclass
from pathlib import Path

API = "https://api.usaspending.gov/api/v2"
ARCHIVE = "https://files.usaspending.gov/award_data_archive"

AGENCY_CODE = "036"                       # Department of Veterans Affairs
AGENCY_NAME = "Department of Veterans Affairs"
FISCAL_YEARS = range(2019, 2026)          # complete federal fiscal years (Oct 1 to Sep 30)
FALLBACK_ARCHIVE_DATE = "20261106"        # used when the file listing endpoint is down

# Competition, as recorded in FPDS "extent competed"
NOT_COMPETED = ("B", "C", "G")            # not available for competition, not competed, not competed under SAP
COMPETED = ("A", "D", "E", "F", "CDO")    # full and open, after exclusion of sources, follow-on, competed under SAP, competitive delivery order

GROWTH_FLAG = 0.5                         # value grew more than 50% after award
MIN_GROWTH_BASE = 100_000                 # ignore growth on awards whose first value was under $100k
DEPENDENCE_SHARE = 0.5                    # one supplier holds more than half of a category's spend
MIN_CATEGORY_SPEND = 25_000_000           # only judge concentration in categories this large over the period


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def archives(self) -> Path:
        return self.data / "bronze" / "archives"

    @property
    def bronze(self) -> Path:
        return self.data / "bronze"

    @property
    def warehouse(self) -> Path:
        return self.data / "warehouse.duckdb"

    @property
    def export(self) -> Path:
        return self.root / "docs" / "data" / "pulse.json"
