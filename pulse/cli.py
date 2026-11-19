"""pulse run | ingest | build"""
import argparse
import sys
import time
from pathlib import Path

from . import analysis, config, export, ingest, quality, transform


def build(paths: config.Paths) -> int:
    started = time.time()
    con = transform.build(paths)
    analysis.build_gold(con)
    print(f"gold built in {time.time() - started:.0f}s")
    checks = quality.run(con)
    if not quality.report(checks):
        print("critical data-quality failure: nothing exported", file=sys.stderr)
        return 1
    export.export(con, paths, checks)
    print(f"exported {paths.export.relative_to(paths.root)}")
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="pulse", description=__doc__)
    p.add_argument("command", choices=["run", "ingest", "build"])
    p.add_argument("--root", type=Path, default=Path.cwd())
    args = p.parse_args(argv)
    paths = config.Paths(args.root.resolve())
    if args.command in ("run", "ingest"):
        ingest.ingest(paths)
    code = build(paths) if args.command in ("run", "build") else 0
    raise SystemExit(code)
