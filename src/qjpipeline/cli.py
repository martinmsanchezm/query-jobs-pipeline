"""Command line: `qjp demo`, `qjp run`, `qjp report`."""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import store
from .pipeline import ROOT, RunConfig, run_pipeline
from .report import build_report

SAMPLES = ROOT / "data" / "samples"
# The demo replays two saved days with a fixed clock, so its output is reproducible.
DEMO_DAYS = (("day1", datetime(2026, 9, 29, 12, 0)), ("day2", datetime(2026, 9, 30, 12, 0)))


def _print(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    sys.stdout.flush()


def cmd_demo(args: argparse.Namespace) -> int:
    out = Path(args.output)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    _print(f"Offline demo: two runs over saved API payloads in {SAMPLES}\n")
    _print(f"AI enrichment: {args.ai}. Output: {out}\n\n")
    for day, clock in DEMO_DAYS:
        summary = run_pipeline(
            RunConfig(
                db_path=out / "jobs.duckdb",
                output_dir=out,
                samples_dir=SAMPLES / day,
                ai_mode=args.ai,
                now=clock,
            )
        )
        _print(f"{'=' * 78}\n{day}: {summary.report_path.name}\n{'=' * 78}\n")
        _print(summary.report + "\n")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.output)
    summary = run_pipeline(
        RunConfig(
            db_path=Path(args.db) if args.db else out / "jobs.duckdb",
            output_dir=out,
            samples_dir=Path(args.samples) if args.samples else None,
            live_config=Path(args.config) if args.live else None,
            ai_mode=args.ai,
            enrich_limit=args.enrich_limit,
        )
    )
    _print(summary.report)
    _print(f"\nReport saved to {summary.report_path}\n")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    con = store.connect(args.db)
    run_id = args.run or con.execute("SELECT max(run_id) FROM runs").fetchone()[0]
    if run_id is None:
        _print("No runs yet.\n")
        return 1
    _print(build_report(con, run_id))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qjp", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="offline demo over saved payloads (no network)")
    demo.add_argument("--output", default="output/demo")
    demo.add_argument("--ai", default="simulated", help="off | simulated | claude")
    demo.set_defaults(func=cmd_demo)

    run = sub.add_parser("run", help="run the pipeline once")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--live", action="store_true", help="query the real public APIs")
    source.add_argument("--samples", help="directory of saved payloads, e.g. data/samples/day1")
    run.add_argument("--config", default=str(ROOT / "config" / "live.toml"))
    run.add_argument("--output", default="output/live")
    run.add_argument("--db", help="DuckDB file (default: <output>/jobs.duckdb)")
    run.add_argument("--ai", default=None, help="off | simulated | claude | auto (default)")
    run.add_argument("--enrich-limit", type=int, default=None, help="cap AI calls per run")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="print the report of a stored run")
    report.add_argument("--db", default="output/live/jobs.duckdb")
    report.add_argument("--run", type=int, default=None, help="run id (default: latest)")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
