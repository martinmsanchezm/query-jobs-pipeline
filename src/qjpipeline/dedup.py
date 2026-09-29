"""Deduplication: one job, however many sources and runs publish it.

Two postings are the same job when they share
  * the canonical URL (tracking parameters and cosmetic differences removed), or
  * the dedup key: normalized company + normalized title.

Within a batch, postings are grouped with union-find over both keys. Across
runs, the store matches a group to an existing job by source id, canonical URL
or dedup key (see store.py). The merged job takes each field from the most
trustworthy source that has it: the employer's own ATS board first, then the
aggregators.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Posting
from .text import fold

# Employer ATS boards are the primary record; aggregators are copies of it.
SOURCE_PRIORITY = ("greenhouse", "lever", "ashby", "himalayas", "remotive", "jobicy", "remoteok")

_TRACKING = frozenset(
    [
        "ref",
        "refs",
        "referrer",
        "source",
        "src",
        "via",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "trk",
        "gh_src",
        "lever-source",
        "lever-origin",
    ]
)
_LEGAL_SUFFIXES = frozenset(
    [
        "inc",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "ag",
        "sa",
        "sas",
        "sl",
        "srl",
        "bv",
        "plc",
        "pty",
        "spa",
        "ltda",
    ]
)
_TITLE_NOISE = frozenset(["remote", "fully", "100", "anywhere", "worldwide", "latam", "hiring"])


def canonical_url(url: str) -> str:
    """Same page, same string: lowercase host without www, no fragment, no trailing
    slash, no tracking parameters, remaining parameters sorted."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def normalize_company(company: str | None) -> str:
    return " ".join(w for w in fold(company).split() if w not in _LEGAL_SUFFIXES)


def normalize_title(title: str) -> str:
    # "(Remote - LATAM)" and "[US only]" describe the posting, not the job.
    without_notes = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title)
    return " ".join(w for w in fold(without_notes).split() if w not in _TITLE_NOISE)


def dedup_key(company: str | None, title: str) -> str | None:
    """None when the company is unknown: a title alone is too weak to merge on."""
    company_part = normalize_company(company)
    if not company_part:
        return None
    return f"{company_part}|{normalize_title(title)}"


_MERGED = tuple(
    f.name for f in fields(Posting) if f.name not in ("source", "source_job_id", "url", "raw")
)


@dataclass
class JobGroup:
    """Every posting of one job found in this batch, plus the merged view."""

    postings: list[Posting]
    canonical_url: str
    dedup_key: str | None
    merged: dict[str, object] = field(default_factory=dict)

    @property
    def links(self) -> list[tuple[str, str, str]]:
        return [(p.source, p.source_job_id, p.url) for p in self.postings]


def _rank(posting: Posting) -> int:
    try:
        return SOURCE_PRIORITY.index(posting.source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def merge_postings(postings: list[Posting]) -> dict[str, object]:
    """Field by field, the value from the highest-priority source that has one."""
    ordered = sorted(postings, key=_rank)
    merged: dict[str, object] = {}
    for name in _MERGED:
        merged[name] = next((getattr(p, name) for p in ordered if getattr(p, name)), None)
    return merged


def group_batch(postings: list[Posting]) -> list[JobGroup]:
    """Group postings that describe the same job (union-find over URL and key)."""
    # The same source can return one job twice (e.g. two role searches).
    unique: dict[tuple[str, str], Posting] = {}
    for posting in postings:
        unique.setdefault((posting.source, posting.source_job_id), posting)
    items = list(unique.values())

    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_seen: dict[str, int] = {}
    for i, posting in enumerate(items):
        for key in (
            f"url:{canonical_url(posting.url)}",
            f"key:{dedup_key(posting.company, posting.title)}",
        ):
            if key == "key:None":
                continue
            if key in first_seen:
                parent[find(i)] = find(first_seen[key])
            else:
                first_seen[key] = i

    buckets: dict[int, list[Posting]] = {}
    for i, posting in enumerate(items):
        buckets.setdefault(find(i), []).append(posting)

    groups = []
    for members in buckets.values():
        members.sort(key=_rank)
        primary = members[0]
        groups.append(
            JobGroup(
                postings=members,
                canonical_url=canonical_url(primary.url),
                dedup_key=dedup_key(primary.company, primary.title),
                merged=merge_postings(members),
            )
        )
    return groups
