"""Incremental loading into DuckDB."""

from datetime import datetime, timedelta

from qjpipeline import store
from qjpipeline.dedup import group_batch
from qjpipeline.pipeline import extract, sample_payloads

from .conftest import DAY1, DAY2, SAMPLES, posting


def load_day(con, day, now):
    payloads = sample_payloads(SAMPLES / day, SAMPLES / "boards.toml")
    kept, _rejections, _stats, _filtered = extract(payloads, now)
    run_id = store.start_run(con, "samples", now)
    return store.load_groups(con, run_id, group_batch(kept), now)


def count(con, sql):
    return con.execute(sql).fetchone()[0]


def test_two_days_load_incrementally(tmp_path):
    con = store.connect(tmp_path / "jobs.duckdb")
    first = load_day(con, "day1", DAY1)
    assert (first.new_jobs, first.seen_again) == (11, 0)
    second = load_day(con, "day2", DAY2)
    assert (second.new_jobs, second.seen_again) == (4, 7)
    assert count(con, "SELECT COUNT(*) FROM jobs") == 15
    # Jobs missing from day 2 stay stored; they are just not seen again.
    assert count(con, "SELECT COUNT(*) FROM jobs WHERE last_run_id = 1") == 4
    assert store.verify(con) == {}


def test_reloading_the_same_input_inserts_nothing(tmp_path):
    con = store.connect(tmp_path / "jobs.duckdb")
    load_day(con, "day1", DAY1)
    again = load_day(con, "day1", DAY1 + timedelta(hours=1))
    assert (again.new_jobs, again.seen_again, again.new_links) == (0, 11, 0)
    assert count(con, "SELECT COUNT(*) FROM jobs") == 11


def test_a_job_seen_on_a_new_source_gets_a_link_not_a_row(tmp_path):
    con = store.connect(tmp_path / "jobs.duckdb")
    load_day(con, "day1", DAY1)
    load_day(con, "day2", DAY2)
    sources = con.execute(
        "SELECT list_sort(list(s.source)) FROM jobs j JOIN job_sources s USING (job_id) "
        "WHERE j.company = 'Woodgrove Bank' GROUP BY j.job_id"
    ).fetchall()
    assert sources == [(["himalayas", "remotive"],)]


def test_stored_values_win_and_only_gaps_are_filled(tmp_path):
    con = store.connect(tmp_path / "jobs.duckdb")
    run = store.start_run(con, "samples", DAY1)
    first = posting(source_job_id="1", description="original", salary_text=None)
    store.load_groups(con, run, group_batch([first]), DAY1)
    later = posting(source_job_id="1", description="edited", salary_text="$90k")
    store.load_groups(con, store.start_run(con, "samples", DAY2), group_batch([later]), DAY2)
    description, salary = con.execute("SELECT description, salary_text FROM jobs").fetchone()
    assert (description, salary) == ("original", "$90k")


def test_a_repost_after_the_window_is_a_new_job(tmp_path):
    con = store.connect(tmp_path / "jobs.duckdb")
    old = posting(source="remotive", source_job_id="1", url="https://a.example/1")
    store.load_groups(con, store.start_run(con, "samples", DAY1), group_batch([old]), DAY1)
    much_later = DAY1 + store.SAME_JOB_WINDOW + timedelta(days=1)
    repost = posting(source="jobicy", source_job_id="9", url="https://b.example/9")
    result = store.load_groups(
        con, store.start_run(con, "samples", much_later), group_batch([repost]), much_later
    )
    assert result.new_jobs == 1
    soon = posting(source="jobicy", source_job_id="10", url="https://c.example/10")
    result = store.load_groups(
        con, store.start_run(con, "samples", much_later), group_batch([soon]), much_later
    )
    assert result.new_jobs == 0  # same company and title, inside the window


def test_load_checks_catch_inconsistent_tables(tmp_path):
    con = store.connect(tmp_path / "jobs.duckdb")
    load_day(con, "day1", DAY1)
    con.execute(
        "INSERT INTO job_sources VALUES ('remotive', 'ghost', 'j-missing', 'https://x.example', "
        "?, ?, 1)",
        [datetime(2026, 9, 29), datetime(2026, 9, 29)],
    )
    assert store.verify(con) == {"orphan_source_links": 1}
