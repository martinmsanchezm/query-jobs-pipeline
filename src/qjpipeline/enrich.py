"""Optional AI enrichment: seniority, tech stack and affinity to a profile.

Providers
    off        no enrichment; reports show "-" for these columns
    simulated  deterministic rules, no API key, no cost (demo and tests default)
    claude     Claude through the official Anthropic SDK, with structured output;
               enabled when ANTHROPIC_API_KEY is set (or QJP_AI=claude)

Every provider returns the same `Enrichment` shape, so the rest of the
pipeline does not know or care which one ran. Enrichment is incremental: a
job is (re)processed only when its content, the profile or the provider
changed since it was last enriched.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import duckdb

from .profile import LEVELS, Profile
from .text import fold

SENIORITY = (*LEVELS, "unknown")

# Canonical stack terms and the spellings that map to them.
STACK_ALIASES: dict[str, tuple[str, ...]] = {
    "Python": ("python", "pyspark", "pandas"),
    "SQL": ("sql",),
    "Spark": ("spark", "apache spark", "pyspark"),
    "Airflow": ("airflow", "apache airflow"),
    "dbt": ("dbt",),
    "Kafka": ("kafka", "apache kafka"),
    "Flink": ("flink",),
    "Snowflake": ("snowflake",),
    "BigQuery": ("bigquery", "big query"),
    "Redshift": ("redshift",),
    "Databricks": ("databricks",),
    "PostgreSQL": ("postgres", "postgresql"),
    "AWS": ("aws", "amazon web services"),
    "GCP": ("gcp", "google cloud"),
    "Azure": ("azure",),
    "Terraform": ("terraform",),
    "Docker": ("docker",),
    "Kubernetes": ("kubernetes", "k8s"),
    "Scala": ("scala",),
    "Java": ("java",),
    "Go": ("golang",),
    "Dagster": ("dagster",),
    "Fivetran": ("fivetran",),
    "Looker": ("looker",),
}


@dataclass(frozen=True)
class Enrichment:
    seniority: str
    stack: tuple[str, ...]
    affinity: int
    rationale: str


class EnrichmentError(Exception):
    """One job could not be enriched; the batch continues."""


class ProviderUnavailable(Exception):
    """The provider cannot work at all (e.g. invalid API key); the batch stops."""


class Provider(Protocol):
    name: str
    model: str

    def enrich(self, job: dict[str, Any], profile: Profile) -> Enrichment: ...


# ── Simulated provider ──────────────────────────────────────


def detect_stack(text: str) -> tuple[str, ...]:
    padded = f" {fold(text)} "
    found = [
        term
        for term, spellings in STACK_ALIASES.items()
        if any(f" {fold(s)} " in padded for s in spellings)
    ]
    return tuple(found)


def detect_seniority(title: str, description: str | None) -> str:
    words = set(fold(title).split())
    if words & {"intern", "junior", "jr", "entry", "graduate"}:
        return "junior"
    if words & {"staff", "principal", "lead", "head", "manager", "architect"}:
        return "lead"
    if words & {"senior", "sr"}:
        return "senior"
    if words & {"ii", "2", "mid"}:
        return "mid"
    years = [int(y) for y in re.findall(r"(\d{1,2})\+?\s*years", (description or "").lower())]
    if years:
        most = max(years)
        return "junior" if most <= 2 else "mid" if most <= 4 else "senior"
    return "unknown"


def affinity_score(
    stack: tuple[str, ...], seniority: str, salary_max: int | None, profile: Profile
) -> tuple[int, str]:
    """0-100: 60% stack overlap, 25% seniority fit, 15% salary meets the minimum."""
    mine = {s.lower() for s in profile.skills}
    matched = [s for s in stack if s.lower() in mine]
    missing = [s for s in stack if s.lower() not in mine]
    stack_fit = len(matched) / len(stack) if stack else 0.5
    if seniority == "unknown":
        level_fit = 0.5
    else:
        distance = abs(LEVELS.index(seniority) - LEVELS.index(profile.seniority))
        level_fit = (1.0, 0.6, 0.2, 0.0)[distance]
    if salary_max is None or profile.min_salary_usd is None:
        pay_fit, pay_note = 0.5, "salary not stated"
    else:
        ok = salary_max >= profile.min_salary_usd
        pay_fit, pay_note = (1.0 if ok else 0.2), f"salary {'meets' if ok else 'below'} minimum"
    score = round(100 * (0.6 * stack_fit + 0.25 * level_fit + 0.15 * pay_fit))
    rationale = (
        f"Stack match: {', '.join(matched) or 'none'}; missing: {', '.join(missing) or 'none'}. "
        f"Seniority {seniority} vs target {profile.seniority}; {pay_note}."
    )
    return score, rationale


class SimulatedProvider:
    """Rule-based stand-in with the same output contract as the AI provider."""

    name = "simulated"
    model = "rules-v1"

    def enrich(self, job: dict[str, Any], profile: Profile) -> Enrichment:
        text = f"{job['title']}\n{job.get('description') or ''}"
        stack = detect_stack(text)
        seniority = detect_seniority(job["title"], job.get("description"))
        score, rationale = affinity_score(stack, seniority, job.get("salary_max"), profile)
        return Enrichment(seniority, stack, score, rationale)


# ── Claude provider ─────────────────────────────────────────

DEFAULT_MODEL = "claude-opus-5-5"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "seniority": {"type": "string", "enum": list(SENIORITY)},
        "stack": {"type": "array", "items": {"type": "string"}},
        "affinity": {"type": "integer"},
        "rationale": {"type": "string"},
    },
    "required": ["seniority", "stack", "affinity", "rationale"],
    "additionalProperties": False,
}

SYSTEM_TEMPLATE = """You classify remote job postings for a data engineering candidate.

For each posting, return:
- seniority: the level the posting asks for ({levels}); "unknown" when the posting does not say.
- stack: the concrete technologies the posting requires or prefers (languages, databases, \
orchestration, cloud, streaming). Use common canonical names such as {examples}. Omit soft skills.
- affinity: an integer from 0 to 100 for how well the candidate below fits the posting's \
must-have requirements and seniority. 80 or more is a strong fit, 50-79 partial, under 50 weak.
- rationale: one or two sentences naming the matched and the missing requirements.

Use only the candidate profile below as evidence about the candidate. Do not assume skills \
the profile does not list.

Candidate profile (JSON):
{profile}"""


@dataclass
class ClaudeProvider:
    """Claude through the official Anthropic SDK.

    * Structured output (`output_config.format`) guarantees parseable JSON.
    * `effort: low`: a short classification does not need deep reasoning.
    * The system prompt (rules + profile) is identical for every job and marked
      for prompt caching.
    * `fallbacks: "default"` lets the API retry a declined request on its
      recommended fallback model instead of returning the refusal.
    """

    model: str = DEFAULT_MODEL
    client: Any = None
    name: str = field(default="claude", init=False)

    def __post_init__(self) -> None:
        if self.client is None:
            try:
                import anthropic
            except ImportError as exc:  # optional dependency
                raise ProviderUnavailable(
                    "the anthropic package is not installed: pip install '.[ai]'"
                ) from exc
            self.client = anthropic.Anthropic()

    def system_prompt(self, profile: Profile) -> str:
        return SYSTEM_TEMPLATE.format(
            levels=", ".join(SENIORITY),
            examples=", ".join(list(STACK_ALIASES)[:8]),
            profile=json.dumps(profile.as_dict(), sort_keys=True),
        )

    def enrich(self, job: dict[str, Any], profile: Profile) -> Enrichment:
        posting = {
            k: job.get(k) for k in ("title", "company", "location", "salary_text", "description")
        }
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
                system=[
                    {
                        "type": "text",
                        "text": self.system_prompt(profile),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": json.dumps(posting, ensure_ascii=False)}],
            )
        except Exception as exc:
            raise _translate(exc) from exc
        if response.stop_reason == "refusal":
            raise EnrichmentError("the model declined to classify this posting")
        if response.stop_reason == "max_tokens":
            raise EnrichmentError("the response was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise EnrichmentError("the response had no text block")
        return parse_enrichment(json.loads(text))


def _translate(exc: Exception) -> Exception:
    """Map SDK errors to: stop the batch (credentials) or skip this job (the rest)."""
    try:
        import anthropic
    except ImportError:
        return EnrichmentError(str(exc))
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderUnavailable(f"Anthropic rejected the credentials: {exc}")
    if isinstance(exc, anthropic.RateLimitError):
        return EnrichmentError("rate limited after the SDK's retries")
    if isinstance(exc, anthropic.APIStatusError):
        return EnrichmentError(f"API error {exc.status_code}: {exc.message}")
    if isinstance(exc, anthropic.APIConnectionError):
        return EnrichmentError("could not reach the Anthropic API")
    return EnrichmentError(str(exc))


def parse_enrichment(data: dict[str, Any]) -> Enrichment:
    """Validate a model answer before it reaches the database."""
    seniority = data.get("seniority")
    if seniority not in SENIORITY:
        raise EnrichmentError(f"invalid seniority {seniority!r}")
    affinity = data.get("affinity")
    if not isinstance(affinity, int) or not 0 <= affinity <= 100:
        raise EnrichmentError(f"invalid affinity {affinity!r}")
    canonical = {fold(alias): term for term, aliases in STACK_ALIASES.items() for alias in aliases}
    canonical.update({fold(term): term for term in STACK_ALIASES})
    stack: list[str] = []
    for item in data.get("stack") or []:
        term = canonical.get(fold(str(item)), str(item).strip())
        if term and term not in stack:
            stack.append(term)
    return Enrichment(seniority, tuple(stack), affinity, str(data.get("rationale", "")).strip())


# ── Provider selection and the incremental batch ─────────────


def choose_provider(mode: str | None = None) -> Provider | None:
    """`off`, `simulated`, `claude`, or `auto` (Claude when a key is configured)."""
    mode = (mode or os.environ.get("QJP_AI") or "auto").lower()
    if mode == "off":
        return None
    if mode == "simulated":
        return SimulatedProvider()
    if mode == "claude" or (mode == "auto" and os.environ.get("ANTHROPIC_API_KEY")):
        return ClaudeProvider(model=os.environ.get("QJP_AI_MODEL", DEFAULT_MODEL))
    if mode == "auto":
        return SimulatedProvider()
    raise ValueError(f"unknown AI mode {mode!r} (off, simulated, claude, auto)")


ENRICHMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS enrichments (
    job_id       VARCHAR PRIMARY KEY,
    content_hash VARCHAR NOT NULL,
    profile_hash VARCHAR NOT NULL,
    provider     VARCHAR NOT NULL,
    model        VARCHAR NOT NULL,
    seniority    VARCHAR NOT NULL,
    stack        VARCHAR[] NOT NULL,
    affinity     INTEGER NOT NULL,
    rationale    VARCHAR,
    enriched_at  TIMESTAMP NOT NULL
);
"""


@dataclass
class EnrichStats:
    candidates: int = 0
    enriched: int = 0
    failed: int = 0
    stopped: str | None = None
    errors: list[str] = field(default_factory=list)


def enrich_jobs(
    con: duckdb.DuckDBPyConnection,
    provider: Provider,
    profile: Profile,
    now,
    limit: int | None = None,
) -> EnrichStats:
    """Enrich jobs that are new or changed for this provider and profile."""
    con.execute(ENRICHMENT_SCHEMA)
    query = """
        SELECT j.job_id, j.content_hash, j.title, j.company, j.location, j.salary_text,
               j.salary_max, j.description
        FROM jobs j LEFT JOIN enrichments e USING (job_id)
        WHERE e.job_id IS NULL OR e.content_hash <> j.content_hash
           OR e.profile_hash <> ? OR e.provider <> ? OR e.model <> ?
        ORDER BY j.first_seen_at DESC, j.job_id
    """
    rows = con.execute(query, [profile.fingerprint, provider.name, provider.model]).fetchall()
    columns = [d[0] for d in con.description]
    jobs = [dict(zip(columns, row, strict=True)) for row in rows][:limit]
    stats = EnrichStats(candidates=len(jobs))
    for job in jobs:
        try:
            result = provider.enrich(job, profile)
        except ProviderUnavailable as exc:
            stats.stopped = str(exc)
            break
        except EnrichmentError as exc:
            stats.failed += 1
            stats.errors.append(f"{job['job_id']}: {exc}")
            continue
        con.execute(
            "INSERT OR REPLACE INTO enrichments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                job["job_id"],
                job["content_hash"],
                profile.fingerprint,
                provider.name,
                provider.model,
                result.seniority,
                list(result.stack),
                result.affinity,
                result.rationale,
                now,
            ],
        )
        stats.enriched += 1
    return stats
