"""Local storage in DuckDB, loaded incrementally.

Tables
    runs          one row per pipeline run (mode, timing, per-run stats)
    jobs          one row per distinct job, across sources and runs
    job_sources   every (source, source_job_id) that published a job
    rejections    records that failed a quality rule, per run
    source_stats  per run and source: payloads, records, rejected, filtered, kept

Incremental load
    Each group of postings from dedup.group_batch is matched to an existing job,
    in this order: a known (source, source_job_id), the canonical URL, or the
    dedup key seen within SAME_JOB_WINDOW. A match refreshes `last_seen_at` and
    only fills fields the job was missing; no match inserts a new job. Running
    the same input twice therefore inserts nothing the second time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from .dedup import JobGroup
from .models import Rejection

# A company re-posting the same title after this long is a new opening.
SAME_JOB_WINDOW = timedelta(days=60)

_JOB_FIELDS = (
    "title",
    "company",
    "location",
    "work_mode",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_text",
    "posted_at",
    "description",
)

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS run_ids START 1;
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY DEFAULT nextval('run_ids'),
    mode        VARCHAR NOT NULL,
    started_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    status      VARCHAR NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id          VARCHAR PRIMARY KEY,
    canonical_url   VARCHAR NOT NULL,
    dedup_key       VARCHAR,
    title           VARCHAR NOT NULL,
    company         VARCHAR NOT NULL,
    location        VARCHAR,
    work_mode       VARCHAR,
    salary_min      BIGINT,
    salary_max      BIGINT,
    salary_currency VARCHAR,
    salary_text     VARCHAR,
    posted_at       TIMESTAMP,
    description     VARCHAR,
    content_hash    VARCHAR NOT NULL,
    first_seen_at   TIMESTAMP NOT NULL,
    last_seen_at    TIMESTAMP NOT NULL,
    first_run_id    INTEGER NOT NULL,
    last_run_id     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS job_sources (
    source        VARCHAR NOT NULL,
    source_job_id VARCHAR NOT NULL,
    job_id        VARCHAR NOT NULL,
    url           VARCHAR NOT NULL,
    first_seen_at TIMESTAMP NOT NULL,
    last_seen_at  TIMESTAMP NOT NULL,
    last_run_id   INTEGER NOT NULL,
    PRIMARY KEY (source, source_job_id)
);
CREATE TABLE IF NOT EXISTS rejections (
    run_id        INTEGER NOT NULL,
    stage         VARCHAR NOT NULL,
    source        VARCHAR NOT NULL,
    source_job_id VARCHAR,
    rule          VARCHAR NOT NULL,
    detail        VARCHAR
);
CREATE TABLE IF NOT EXISTS source_stats (
    run_id   INTEGER NOT NULL,
    source   VARCHAR NOT NULL,
    payloads INTEGER NOT NULL,
    records  INTEGER NOT NULL,
    rejected INTEGER NOT NULL,
    filtered INTEGER NOT NULL,
    kept     INTEGER NOT NULL,
    PRIMARY KEY (run_id, source)
);
"""


@dataclass
class LoadResult:
    new_jobs: int = 0
    seen_again: int = 0
    new_links: int = 0


def connect(path: str | Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con


def start_run(con: duckdb.DuckDBPyConnection, mode: str, now: datetime) -> int:
    row = con.execute(
        "INSERT INTO runs (mode, started_at) VALUES (?, ?) RETURNING run_id", [mode, now]
    ).fetchone()
    return int(row[0])


def finish_run(con: duckdb.DuckDBPyConnection, run_id: int, status: str, now: datetime) -> None:
    con.execute(
        "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?", [status, now, run_id]
    )


def record_rejections(
    con: duckdb.DuckDBPyConnection, run_id: int, rejections: list[Rejection]
) -> None:
    if rejections:
        con.executemany(
            "INSERT INTO rejections VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, r.stage, r.source, r.source_job_id, r.rule, r.detail) for r in rejections],
        )


def record_source_stats(
    con: duckdb.DuckDBPyConnection, run_id: int, stats: dict[str, dict[str, int]]
) -> None:
    con.executemany(
        "INSERT INTO source_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, source, s["payloads"], s["records"], s["rejected"], s["filtered"], s["kept"])
            for source, s in sorted(stats.items())
        ],
    )


def content_hash(values: dict[str, object]) -> str:
    """Fingerprint of what a job says; enrichment reruns only when it changes."""
    payload = {k: values.get(k) for k in ("title", "company", "location", "description")}
    payload["salary_text"] = values.get("salary_text")
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def new_job_id(canonical_url: str) -> str:
    return "j" + hashlib.sha1(canonical_url.encode()).hexdigest()[:15]


def _match(con: duckdb.DuckDBPyConnection, group: JobGroup, now: datetime) -> str | None:
    for source, source_job_id, _url in group.links:
        row = con.execute(
            "SELECT job_id FROM job_sources WHERE source = ? AND source_job_id = ?",
            [source, source_job_id],
        ).fetchone()
        if row:
            return row[0]
    row = con.execute(
        "SELECT job_id FROM jobs WHERE canonical_url = ?", [group.canonical_url]
    ).fetchone()
    if row:
        return row[0]
    if group.dedup_key:
        row = con.execute(
            "SELECT job_id FROM jobs WHERE dedup_key = ? AND last_seen_at >= ? "
            "ORDER BY last_seen_at DESC LIMIT 1",
            [group.dedup_key, now - SAME_JOB_WINDOW],
        ).fetchone()
        if row:
            return row[0]
    return None


def load_groups(
    con: duckdb.DuckDBPyConnection, run_id: int, groups: list[JobGroup], now: datetime
) -> LoadResult:
    """Upsert jobs and their source links in one transaction."""
    result = LoadResult()
    con.execute("BEGIN TRANSACTION")
    try:
        for group in groups:
            job_id = _match(con, group, now)
            if job_id is None:
                job_id = new_job_id(group.canonical_url)
                values = dict(group.merged)
                columns = ", ".join(_JOB_FIELDS)
                con.execute(
                    f"INSERT INTO jobs (job_id, canonical_url, dedup_key, {columns}, "
                    "content_hash, first_seen_at, last_seen_at, first_run_id, last_run_id) "
                    f"VALUES ({', '.join('?' * (len(_JOB_FIELDS) + 8))})",
                    [
                        job_id,
                        group.canonical_url,
                        group.dedup_key,
                        *(values[f] for f in _JOB_FIELDS),
                        content_hash(values),
                        now,
                        now,
                        run_id,
                        run_id,
                    ],
                )
                result.new_jobs += 1
            else:
                current = con.execute(
                    f"SELECT {', '.join(_JOB_FIELDS)} FROM jobs WHERE job_id = ?", [job_id]
                ).fetchone()
                stored = dict(zip(_JOB_FIELDS, current, strict=True))
                # Keep what is stored; only fill what is missing.
                values = {
                    f: stored[f] if stored[f] is not None else group.merged[f] for f in stored
                }
                con.execute(
                    f"UPDATE jobs SET {', '.join(f'{f} = ?' for f in _JOB_FIELDS)}, "
                    "content_hash = ?, last_seen_at = ?, last_run_id = ? WHERE job_id = ?",
                    [*(values[f] for f in _JOB_FIELDS), content_hash(values), now, run_id, job_id],
                )
                result.seen_again += 1
            for source, source_job_id, url in group.links:
                known = con.execute(
                    "SELECT 1 FROM job_sources WHERE source = ? AND source_job_id = ?",
                    [source, source_job_id],
                ).fetchone()
                con.execute(
                    "INSERT INTO job_sources VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (source, source_job_id) DO UPDATE SET "
                    "url = excluded.url, last_seen_at = excluded.last_seen_at, "
                    "last_run_id = excluded.last_run_id",
                    [source, source_job_id, job_id, url, now, now, run_id],
                )
                if not known:
                    result.new_links += 1
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return result


# Load-stage quality checks: each query returns the offending rows (none expected).
LOAD_CHECKS = {
    "jobs_missing_required": "SELECT job_id FROM jobs WHERE title = '' OR company = '' "
    "OR canonical_url = ''",
    "duplicate_canonical_url": "SELECT canonical_url FROM jobs GROUP BY canonical_url "
    "HAVING COUNT(*) > 1",
    "orphan_source_links": "SELECT s.source, s.source_job_id FROM job_sources s "
    "LEFT JOIN jobs j USING (job_id) WHERE j.job_id IS NULL",
    "job_without_sources": "SELECT j.job_id FROM jobs j "
    "LEFT JOIN job_sources s USING (job_id) WHERE s.job_id IS NULL",
}


def verify(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Run the load-stage checks; returns {check: offending rows} for failures only."""
    failures = {}
    for name, query in LOAD_CHECKS.items():
        count = len(con.execute(query).fetchall())
        if count:
            failures[name] = count
    return failures
