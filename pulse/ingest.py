"""Bronze: download the VA's yearly contract archives from USAspending and verify them.

USAspending's servers drop connections without a response for minutes at a time, so every request
retries with backoff, downloads resume where they stopped, and a file is only kept once its size
matches the server's and it opens as a valid zip. Each archive holds every contract action
(new awards and modifications) the agency reported in that fiscal year.
"""
import json
import random
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

from . import config

HEADERS = {"User-Agent": "procurement-pulse/0.1", "Content-Type": "application/json"}


def request(url: str, body: dict | None = None, method: str | None = None, headers: dict | None = None,
            retries: int = 40):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={**HEADERS, **(headers or {})})
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code == 416:          # range past the end: the file is already complete
                return e
            if attempt == retries - 1:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
        # the outages last minutes, so keep polling at a steady pace
        time.sleep(min(30, 2 ** attempt) + random.random() * 2)
    raise AssertionError("unreachable")


def archive_name(fiscal_year: int) -> str:
    """The archive file name carries the date it was generated, which changes every month."""
    try:
        listing = json.loads(request(f"{config.API}/bulk_download/list_agencies/",
                                     {"type": "award_agencies"}, retries=6).read())
        agencies = listing["agencies"]["cfo_agencies"] + listing["agencies"]["other_agencies"]
        agency_id = next(a["toptier_agency_id"] for a in agencies if a["toptier_code"] == config.AGENCY_CODE)
        files = json.loads(request(f"{config.API}/bulk_download/list_monthly_files/",
                                   {"agency": agency_id, "fiscal_year": fiscal_year, "type": "contracts"},
                                   retries=6).read())["monthly_files"]
        return next(f["file_name"] for f in files if "_Full_" in f["file_name"])
    except Exception:
        return f"FY{fiscal_year}_{config.AGENCY_CODE}_Contracts_Full_{config.FALLBACK_ARCHIVE_DATE}.zip"


def is_valid_zip(path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None and any(n.endswith(".csv") for n in z.namelist())
    except zipfile.BadZipFile:
        return False


def download(paths: config.Paths, fiscal_year: int, log=print) -> str:
    paths.archives.mkdir(parents=True, exist_ok=True)
    existing = sorted(paths.archives.glob(f"FY{fiscal_year}_{config.AGENCY_CODE}_Contracts_Full_*.zip"))
    if existing and is_valid_zip(existing[-1]):
        return existing[-1].name
    name = archive_name(fiscal_year)
    target, part = paths.archives / name, paths.archives / f"{name}.part"
    size = int(request(f"{config.ARCHIVE}/{name}", method="HEAD").headers["Content-Length"])
    for _ in range(200):
        have = part.stat().st_size if part.exists() else 0
        if have >= size:
            break
        try:
            r = request(f"{config.ARCHIVE}/{name}", headers={"Range": f"bytes={have}-"})
            if r.status == 200 and have:   # server ignored the range: start over
                part.unlink()
            with open(part, "ab") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
        except Exception as e:
            log(f"  FY{fiscal_year}: connection dropped at {have / 1e6:.0f} of {size / 1e6:.0f} MB, resuming ({e})")
            time.sleep(5)
    if part.stat().st_size != size or not is_valid_zip(part):
        part.unlink(missing_ok=True)
        raise RuntimeError(f"FY{fiscal_year}: download did not verify; run again to retry")
    part.replace(target)
    for old in existing:
        old.unlink()
    log(f"  FY{fiscal_year}: {name}, {size / 1e6:.0f} MB")
    return name


def ingest(paths: config.Paths, log=print) -> None:
    names = [download(paths, fy, log) for fy in config.FISCAL_YEARS]
    (paths.bronze / "archives.json").write_text(json.dumps({
        "agency": config.AGENCY_NAME, "files": names,
        "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2))
    log(f"{len(names)} fiscal years of {config.AGENCY_NAME} contract actions verified")
