"""Run report: what came in, what was rejected, and the best new jobs."""

from __future__ import annotations

import duckdb


def _table(headers: list[str], rows: list[tuple]) -> str:
    def cell(v: object) -> str:
        return "-" if v is None else str(v).replace("|", "/")

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _has_enrichments(con: duckdb.DuckDBPyConnection) -> bool:
    found = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'enrichments'"
    ).fetchone()
    return bool(found[0])


def build_report(con: duckdb.DuckDBPyConnection, run_id: int, top: int = 10) -> str:
    mode, started = con.execute(
        "SELECT mode, started_at FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    new_jobs, seen_again = con.execute(
        "SELECT COUNT(*) FILTER (WHERE first_run_id = ?), "
        "COUNT(*) FILTER (WHERE last_run_id = ? AND first_run_id < ?) FROM jobs",
        [run_id, run_id, run_id],
    ).fetchone()
    total_jobs = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    parts = [
        f"# Run {run_id} ({mode}, {started:%Y-%m-%d %H:%M} UTC)",
        "",
        f"**{new_jobs} new jobs**, {seen_again} seen again, {total_jobs} jobs in the database.",
        "",
        "## By source",
        "",
    ]
    by_source = con.execute(
        """
        SELECT st.source, st.payloads, st.records, st.rejected, st.filtered, st.kept,
               COUNT(DISTINCT s.job_id) FILTER (WHERE j.first_run_id = st.run_id) AS new_jobs
        FROM source_stats st
        LEFT JOIN job_sources s ON s.source = st.source AND s.last_run_id = st.run_id
        LEFT JOIN jobs j ON j.job_id = s.job_id
        WHERE st.run_id = ?
        GROUP BY ALL ORDER BY st.source
        """,
        [run_id],
    ).fetchall()
    parts.append(
        _table(
            ["source", "payloads", "records", "rejected", "filtered", "kept", "new jobs"],
            by_source,
        )
    )

    enriched = _has_enrichments(con)
    parts += ["", "## Top new jobs", ""]
    if enriched:
        rows = con.execute(
            """
            SELECT j.title, j.company, e.seniority, e.affinity,
                   array_to_string(e.stack, ', '), j.salary_text,
                   string_agg(DISTINCT s.source, ', ' ORDER BY s.source)
            FROM jobs j
            JOIN job_sources s USING (job_id)
            LEFT JOIN enrichments e USING (job_id)
            WHERE j.first_run_id = ?
            GROUP BY j.job_id, j.title, j.company, e.seniority, e.affinity, e.stack,
                     j.salary_text, j.posted_at
            ORDER BY e.affinity DESC NULLS LAST, j.posted_at DESC NULLS LAST, j.title
            LIMIT ?
            """,
            [run_id, top],
        ).fetchall()
        headers = ["title", "company", "seniority", "affinity", "stack", "salary", "sources"]
    else:
        rows = con.execute(
            """
            SELECT j.title, j.company, j.salary_text,
                   string_agg(DISTINCT s.source, ', ' ORDER BY s.source)
            FROM jobs j JOIN job_sources s USING (job_id)
            WHERE j.first_run_id = ?
            GROUP BY j.job_id, j.title, j.company, j.salary_text, j.posted_at
            ORDER BY j.posted_at DESC NULLS LAST, j.title LIMIT ?
            """,
            [run_id, top],
        ).fetchall()
        headers = ["title", "company", "salary", "sources"]
    parts.append(_table(headers, rows) if rows else "_No new jobs in this run._")

    if enriched:
        stack = con.execute(
            """
            SELECT term, COUNT(*) AS jobs FROM (
                SELECT UNNEST(e.stack) AS term FROM jobs j JOIN enrichments e USING (job_id)
                WHERE j.last_run_id = ?
            ) GROUP BY term ORDER BY jobs DESC, term LIMIT 12
            """,
            [run_id],
        ).fetchall()
        parts += ["", "## Stack demand (jobs seen in this run)", ""]
        parts.append(_table(["technology", "jobs"], stack) if stack else "_No stack data._")

    rejected = con.execute(
        """
        SELECT stage, rule, COUNT(*) AS records, string_agg(DISTINCT source, ', '),
               any_value(detail)
        FROM rejections WHERE run_id = ?
        GROUP BY stage, rule ORDER BY stage, records DESC, rule
        """,
        [run_id],
    ).fetchall()
    parts += ["", "## Rejected by data quality rules", ""]
    parts.append(
        _table(["stage", "rule", "records", "sources", "example"], rejected)
        if rejected
        else "_Nothing rejected._"
    )
    return "\n".join(parts) + "\n"
