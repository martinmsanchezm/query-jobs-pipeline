"""Data quality rules, applied at each stage before data moves on.

A record that breaks a rule is not fixed or guessed: it is rejected with the
rule name and a reason, and the run report lists every rejection. The rules
are deliberately strict about identity and links (a posting without a valid
URL is useless downstream) and lenient about optional fields.

    extract    the raw record is an object with the keys its parser needs
    normalize  required fields, valid URL, plausible dates, coherent salary
    load       consistency checks on the stored tables (see store.verify)
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from .models import Posting, Rejection

EARLIEST_POSTING = datetime(2000, 1, 1)
CLOCK_SKEW = timedelta(days=1)  # sources in other time zones may publish "tomorrow"
SALARY_RANGE = (1_000, 2_000_000)  # yearly amounts outside this are data errors
_CURRENCY = re.compile(r"^[A-Z]{3}$")

Rule = Callable[[Posting, datetime], str | None]


def check_record(source: str, record: Any, required_keys: tuple[str, ...]) -> Rejection | None:
    """Extract stage: the raw record must be an object with non-empty required keys."""
    if not isinstance(record, dict):
        return Rejection("extract", source, None, "not_an_object", type(record).__name__)
    missing = [key for key in required_keys if record.get(key) in (None, "")]
    if missing:
        ident = record.get("id") or record.get("guid")
        return Rejection(
            "extract",
            source,
            None if ident is None else str(ident),
            "missing_raw_field",
            ", ".join(missing),
        )
    return None


def _required_fields(posting: Posting, _now: datetime) -> str | None:
    missing = [
        name for name in ("source_job_id", "title", "company", "url") if not getattr(posting, name)
    ]
    return f"missing_required_field: {', '.join(missing)}" if missing else None


def _valid_url(posting: Posting, _now: datetime) -> str | None:
    if not posting.url:
        return None  # already reported as a missing field
    parts = urlsplit(posting.url)
    if parts.scheme not in ("http", "https") or "." not in parts.netloc or " " in posting.url:
        return f"invalid_url: {posting.url[:120]}"
    return None


def _valid_date(posting: Posting, now: datetime) -> str | None:
    posted = posting.posted_at
    if posted is None:
        return None  # optional: some APIs omit it
    if posted < EARLIEST_POSTING or posted > now + CLOCK_SKEW:
        return f"invalid_date: {posted.isoformat()}"
    return None


def _valid_salary(posting: Posting, _now: datetime) -> str | None:
    low, high = posting.salary_min, posting.salary_max
    for value in (low, high):
        if value is not None and not SALARY_RANGE[0] <= value <= SALARY_RANGE[1]:
            return f"invalid_salary: {value} outside {SALARY_RANGE}"
    if low is not None and high is not None and low > high:
        return f"invalid_salary: min {low} > max {high}"
    if posting.salary_currency and not _CURRENCY.match(posting.salary_currency):
        return f"invalid_salary: currency {posting.salary_currency!r}"
    return None


NORMALIZE_RULES: tuple[Rule, ...] = (_required_fields, _valid_url, _valid_date, _valid_salary)


def check_posting(posting: Posting, now: datetime) -> Rejection | None:
    """Normalize stage: the first rule that fails rejects the posting."""
    for rule in NORMALIZE_RULES:
        problem = rule(posting, now)
        if problem:
            name, _, detail = problem.partition(": ")
            return Rejection("normalize", posting.source, posting.source_job_id, name, detail)
    return None
