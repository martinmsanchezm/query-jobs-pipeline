"""Relevance filter: keep remote postings for the configured roles.

This is not a quality rule. An on-site office manager posting is valid data,
just not what this pipeline is looking for, so it is counted as filtered,
not rejected.
"""

from __future__ import annotations

from .models import Posting
from .text import fold

DEFAULT_ROLES = (
    "Data Engineer",
    "Analytics Engineer",
    "Data Platform Engineer",
    "ETL Developer",
    "Big Data Engineer",
)


def title_matches(title: str, role: str) -> bool:
    """Every word of the role starts some word of the title.

    "Data Engineer" matches "Senior Data Engineer II" and "Data Engineering Lead".
    Word order is ignored, so "Software Engineer, Data Products" matches too: the
    filter favours recall, and enrichment scores the borderline cases.
    """
    words = fold(title).split()
    needed = fold(role).split()
    return bool(needed) and all(any(w.startswith(n) for w in words) for n in needed)


def filter_reason(posting: Posting, roles: tuple[str, ...] = DEFAULT_ROLES) -> str | None:
    """Why the posting is out of scope, or None to keep it."""
    if posting.work_mode != "remote":
        return f"not_remote ({posting.work_mode or 'unknown'})"
    if not any(title_matches(posting.title, role) for role in roles):
        return "other_role"
    return None
