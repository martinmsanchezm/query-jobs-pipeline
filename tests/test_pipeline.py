"""End to end over the sample payloads, and the polite HTTP client."""

import httpx
import pytest

from qjpipeline.http import USER_AGENT, PoliteHttp, SourceError
from qjpipeline.pipeline import RunConfig, run_pipeline

from .conftest import DAY1, DAY2, SAMPLES


def run(tmp_path, day, now, ai="simulated"):
    return run_pipeline(
        RunConfig(
            db_path=tmp_path / "jobs.duckdb",
            output_dir=tmp_path,
            samples_dir=SAMPLES / day,
            ai_mode=ai,
            now=now,
        )
    )


def test_demo_days_end_to_end(tmp_path):
    first = run(tmp_path, "day1", DAY1)
    assert (first.new_jobs, first.seen_again, first.rejected, first.filtered) == (11, 0, 5, 3)
    assert first.enrich.enriched == 11
    second = run(tmp_path, "day2", DAY2)
    assert (second.new_jobs, second.seen_again, second.rejected) == (4, 7, 0)
    assert second.enrich.enriched == 4  # only the new jobs
    assert second.report_path.exists()
    for section in ("## By source", "## Top new jobs", "## Stack demand", "## Rejected"):
        assert section in first.report


def test_rejections_are_reported_with_their_rule(tmp_path):
    report = run(tmp_path, "day1", DAY1).report
    for rule in ("missing_raw_field", "invalid_date", "invalid_salary", "invalid_url"):
        assert rule in report


def test_the_pipeline_runs_without_enrichment(tmp_path):
    summary = run(tmp_path, "day1", DAY1, ai="off")
    assert summary.enrich is None
    assert "affinity" not in summary.report


def test_http_client_retries_and_identifies_itself():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"jobs": []})

    waits = []
    http = PoliteHttp(transport=httpx.MockTransport(handler), sleep=waits.append)
    assert http.get_json("https://api.example/jobs") == {"jobs": []}
    assert len(calls) == 2 and 7.0 in waits
    assert calls[0].headers["User-Agent"] == USER_AGENT


def test_http_client_gives_up_on_client_errors():
    http = PoliteHttp(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), sleep=lambda s: None
    )
    with pytest.raises(SourceError):
        http.get_json("https://api.example/missing")
