"""Enrichment: simulated provider, incremental batches, and the Claude provider
exercised with a fake client (no network, no API key)."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from qjpipeline import enrich, store
from qjpipeline.dedup import group_batch
from qjpipeline.enrich import (
    ClaudeProvider,
    EnrichmentError,
    SimulatedProvider,
    choose_provider,
    detect_seniority,
    detect_stack,
    enrich_jobs,
    parse_enrichment,
)
from qjpipeline.pipeline import DEFAULT_PROFILE
from qjpipeline.profile import load_profile

from .conftest import DAY1, DAY2, posting

PROFILE = load_profile(DEFAULT_PROFILE)


def test_the_example_profile_is_fictional():
    assert "fictional" in PROFILE.name.lower()


def test_simulated_provider_is_deterministic():
    job = {
        "title": "Senior Data Engineer",
        "description": "Python, Apache Airflow and dbt on AWS. 5+ years.",
        "salary_max": 120000,
    }
    first = SimulatedProvider().enrich(job, PROFILE)
    assert first == SimulatedProvider().enrich(job, PROFILE)
    assert first.seniority == "senior"
    assert first.stack == ("Python", "Airflow", "dbt", "AWS")
    assert first.affinity == 100


def test_detection_rules():
    assert detect_stack("PySpark and BigQuery, some Go via golang") == (
        "Python",
        "Spark",
        "BigQuery",
        "Go",
    )
    assert detect_seniority("Junior Data Engineer", None) == "junior"
    assert detect_seniority("Staff Data Engineer", None) == "lead"
    assert detect_seniority("Data Engineer", "3+ years of experience") == "mid"
    assert detect_seniority("Data Engineer", None) == "unknown"


def make_db(tmp_path, *postings):
    con = store.connect(tmp_path / "jobs.duckdb")
    run = store.start_run(con, "samples", DAY1)
    store.load_groups(con, run, group_batch(list(postings)), DAY1)
    return con


def test_enrichment_is_incremental(tmp_path):
    con = make_db(
        tmp_path,
        posting(source_job_id="1", url="https://a.example/1", description="Python"),
        posting(source_job_id="2", url="https://a.example/2", title="Analytics Engineer"),
    )
    provider = SimulatedProvider()
    assert enrich_jobs(con, provider, PROFILE, DAY1).enriched == 2
    assert enrich_jobs(con, provider, PROFILE, DAY1).enriched == 0  # nothing changed

    con.execute("UPDATE jobs SET content_hash = 'changed' WHERE title = 'Analytics Engineer'")
    assert enrich_jobs(con, provider, PROFILE, DAY2).enriched == 1  # content changed

    other = dataclasses.replace(PROFILE, skills=("Python",))
    assert enrich_jobs(con, provider, other, DAY2).enriched == 2  # profile changed


def test_choose_provider(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("QJP_AI", raising=False)
    assert choose_provider("off") is None
    assert isinstance(choose_provider("simulated"), SimulatedProvider)
    assert isinstance(choose_provider(None), SimulatedProvider)  # auto without a key
    with pytest.raises(ValueError):
        choose_provider("gpt")


# ── Claude provider with a fake client ──────────────────────


class FakeMessages:
    def __init__(self, reply=None, stop_reason="end_turn", error=None):
        self.reply, self.stop_reason, self.error = reply, stop_reason, error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        content = [SimpleNamespace(type="thinking", thinking="")]
        if self.reply is not None:
            content.append(SimpleNamespace(type="text", text=json.dumps(self.reply)))
        return SimpleNamespace(stop_reason=self.stop_reason, content=content)


def claude_with(messages: FakeMessages) -> ClaudeProvider:
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return ClaudeProvider(client=client)


JOB = {"title": "Senior Data Engineer", "company": "Contoso", "description": "Spark, k8s"}
GOOD = {
    "seniority": "senior",
    "stack": ["PySpark", "k8s", "Spark", "Iceberg"],
    "affinity": 74,
    "rationale": "Matches Spark; missing Iceberg.",
}


def test_claude_request_shape_and_parsing():
    messages = FakeMessages(reply=GOOD)
    result = claude_with(messages).enrich(JOB, PROFILE)
    assert result.seniority == "senior" and result.affinity == 74
    assert result.stack == ("Spark", "Kubernetes", "Iceberg")  # canonicalized, deduplicated

    call = messages.calls[0]
    assert call["model"] == enrich.DEFAULT_MODEL
    assert call["output_config"]["format"]["schema"] == enrich.OUTPUT_SCHEMA
    assert call["output_config"]["effort"] == "low"
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    system = call["system"][0]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert "Alex Rivera" in system["text"]  # the profile travels in the cached system prompt
    assert json.loads(call["messages"][0]["content"])["title"] == "Senior Data Engineer"


def test_claude_system_prompt_is_stable_for_caching():
    provider = claude_with(FakeMessages(reply=GOOD))
    assert provider.system_prompt(PROFILE) == provider.system_prompt(PROFILE)


def test_claude_refusals_and_bad_answers_fail_one_job():
    with pytest.raises(EnrichmentError):
        claude_with(FakeMessages(stop_reason="refusal")).enrich(JOB, PROFILE)
    with pytest.raises(EnrichmentError):
        claude_with(FakeMessages(reply={**GOOD, "affinity": 140})).enrich(JOB, PROFILE)
    with pytest.raises(EnrichmentError):
        parse_enrichment({**GOOD, "seniority": "wizard"})


def _api_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("error", response=httpx.Response(status, request=request), body=None)


def test_invalid_credentials_stop_the_batch_and_other_errors_skip_one_job(tmp_path):
    con = make_db(
        tmp_path,
        posting(source_job_id="1", url="https://a.example/1"),
        posting(source_job_id="2", url="https://a.example/2", title="Analytics Engineer"),
    )
    auth = claude_with(FakeMessages(error=_api_error(anthropic.AuthenticationError, 401)))
    stats = enrich_jobs(con, auth, PROFILE, DAY1)
    assert stats.enriched == 0 and stats.failed == 0 and "credentials" in stats.stopped

    limited = claude_with(FakeMessages(error=_api_error(anthropic.RateLimitError, 429)))
    stats = enrich_jobs(con, limited, PROFILE, DAY1)
    assert stats.failed == 2 and stats.stopped is None
