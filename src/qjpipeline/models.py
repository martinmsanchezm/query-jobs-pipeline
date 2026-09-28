"""The common schema every source is normalized into."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

WorkMode = Literal["remote", "hybrid", "onsite"]
Stage = Literal["extract", "normalize"]


@dataclass
class Posting:
    """One job posting as published by one source, after normalization.

    Dates are naive UTC. Numeric salary columns hold yearly amounts only; other
    periods stay in `salary_text` so they are never mixed with yearly figures.
    """

    source: str
    source_job_id: str
    url: str
    title: str
    company: str | None = None
    location: str | None = None
    work_mode: WorkMode | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    salary_text: str | None = None
    posted_at: datetime | None = None
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Rejection:
    """A record that failed a data quality rule, kept for the run report."""

    stage: Stage
    source: str
    source_job_id: str | None
    rule: str
    detail: str
