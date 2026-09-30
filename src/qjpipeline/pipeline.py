"""The pipeline: extract -> validate -> filter -> deduplicate -> load -> enrich -> report."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import store
from .dedup import group_batch
from .enrich import EnrichStats, choose_provider, enrich_jobs
from .http import PoliteHttp
from .models import Posting, Rejection
from .profile import load_profile
from .quality import check_posting, check_record
from .relevance import DEFAULT_ROLES, filter_reason
from .report import build_report
from .sources import SOURCES, LiveSettings, Payload

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = ROOT / "data" / "profile.example.json"
PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError)


class DataQualityError(Exception):
    """A load-stage check failed: the run is marked failed."""


@dataclass
class RunConfig:
    db_path: Path
    output_dir: Path
    samples_dir: Path | None = None  # offline: saved API payloads
    live_config: Path | None = None  # live: roles and ATS boards to query
    ai_mode: str | None = None  # off | simulated | claude | auto
    profile_path: Path = DEFAULT_PROFILE
    now: datetime | None = None  # fixed clock for reproducible demo runs
    enrich_limit: int | None = None


@dataclass
class RunSummary:
    run_id: int
    new_jobs: int
    seen_again: int
    rejected: int
    filtered: int
    enrich: EnrichStats | None
    report_path: Path
    report: str
    stats: dict[str, dict[str, int]] = field(default_factory=dict)


def _boards(path: Path) -> dict[str, dict[str, str]]:
    return tomllib.loads(path.read_text(encoding="utf-8")).get("boards", {})


def sample_payloads(samples_dir: Path, boards_file: Path) -> Iterator[Payload]:
    """Saved API responses: <samples_dir>/<source>/<key>.json."""
    boards = _boards(boards_file)
    for source_dir in sorted(p for p in samples_dir.iterdir() if p.is_dir()):
        for file in sorted(source_dir.glob("*.json")):
            body = json.loads(file.read_text(encoding="utf-8"))
            company = boards.get(source_dir.name, {}).get(file.stem)
            yield Payload(source_dir.name, file.stem, body, company)


def live_payloads(config_file: Path, raw_dir: Path) -> Iterator[Payload]:
    """Query the real APIs and keep every raw response on disk (lineage)."""
    config = tomllib.loads(config_file.read_text(encoding="utf-8"))
    settings = LiveSettings(roles=tuple(config["roles"]), boards=config.get("boards", {}))
    http = PoliteHttp()
    try:
        for source in SOURCES.values():
            for payload in source.fetch(http, settings):
                target = raw_dir / source.name / f"{_safe(payload.key)}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(payload.body), encoding="utf-8")
                yield payload
    finally:
        http.close()


def _safe(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in key)


def extract(
    payloads: Iterator[Payload], now: datetime
) -> tuple[list[Posting], list[Rejection], dict[str, dict[str, int]], int]:
    """Parse, validate and filter every record. Returns kept postings,
    rejections, per-source counts and the number filtered as irrelevant."""
    kept: list[Posting] = []
    rejections: list[Rejection] = []
    stats: dict[str, dict[str, int]] = {}
    filtered_total = 0
    for payload in payloads:
        source = SOURCES.get(payload.source)
        if source is None:
            continue
        s = stats.setdefault(
            payload.source,
            {"payloads": 0, "records": 0, "rejected": 0, "filtered": 0, "kept": 0},
        )
        s["payloads"] += 1
        try:
            records = source.records(payload)
        except PARSE_ERRORS as exc:
            rejections.append(
                Rejection("extract", payload.source, None, "unreadable_payload", repr(exc))
            )
            s["rejected"] += 1
            continue
        for record in records:
            s["records"] += 1
            problem = check_record(payload.source, record, source.required_keys)
            if problem is None:
                try:
                    posting = source.normalize(record, payload)
                except PARSE_ERRORS as exc:
                    ident = record.get("id") or record.get("guid")
                    problem = Rejection(
                        "extract", payload.source, str(ident), "unparseable", repr(exc)[:200]
                    )
                else:
                    problem = check_posting(posting, now)
            if problem is not None:
                rejections.append(problem)
                s["rejected"] += 1
                continue
            if filter_reason(posting, DEFAULT_ROLES):
                s["filtered"] += 1
                filtered_total += 1
                continue
            kept.append(posting)
            s["kept"] += 1
    return kept, rejections, stats, filtered_total


def run_pipeline(config: RunConfig) -> RunSummary:
    now = config.now or datetime.now(UTC).replace(tzinfo=None)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    con = store.connect(config.db_path)
    mode = "live" if config.live_config else "samples"
    run_id = store.start_run(con, mode, now)
    try:
        if config.live_config:
            raw_dir = config.output_dir / "raw" / f"run-{run_id}"
            payloads = live_payloads(config.live_config, raw_dir)
        else:
            assert config.samples_dir is not None, "samples_dir or live_config is required"
            payloads = sample_payloads(
                config.samples_dir, config.samples_dir.parent / "boards.toml"
            )
        kept, rejections, stats, filtered = extract(payloads, now)
        groups = group_batch(kept)
        store.load_groups(con, run_id, groups, now)
        store.record_rejections(con, run_id, rejections)
        store.record_source_stats(con, run_id, stats)

        failures = store.verify(con)
        if failures:
            raise DataQualityError(f"load-stage checks failed: {failures}")

        enrich_stats = None
        provider = choose_provider(config.ai_mode)
        if provider is not None:
            profile = load_profile(config.profile_path)
            enrich_stats = enrich_jobs(con, provider, profile, now, config.enrich_limit)

        report = build_report(con, run_id)
        if enrich_stats is not None:
            report += (
                f"\n_Enrichment ({provider.name}, {provider.model}): "
                f"{enrich_stats.enriched} enriched, {enrich_stats.failed} failed"
                + (f", stopped: {enrich_stats.stopped}" if enrich_stats.stopped else "")
                + "._\n"
            )
        report_path = config.output_dir / f"report-run-{run_id}.md"
        report_path.write_text(report, encoding="utf-8")
        store.finish_run(con, run_id, "ok", datetime.now(UTC).replace(tzinfo=None))
        new_jobs, seen_again = con.execute(
            "SELECT COUNT(*) FILTER (WHERE first_run_id = ?), "
            "COUNT(*) FILTER (WHERE last_run_id = ? AND first_run_id < ?) FROM jobs",
            [run_id, run_id, run_id],
        ).fetchone()
        return RunSummary(
            run_id=run_id,
            new_jobs=new_jobs,
            seen_again=seen_again,
            rejected=len(rejections),
            filtered=filtered,
            enrich=enrich_stats,
            report_path=report_path,
            report=report,
            stats=stats,
        )
    except BaseException:
        store.finish_run(con, run_id, "failed", datetime.now(UTC).replace(tzinfo=None))
        raise
    finally:
        con.close()
