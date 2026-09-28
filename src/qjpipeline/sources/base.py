"""The source interface.

A source knows three things about one job API: how to fetch its payloads
(live mode), how to find the job records inside a payload, and how to map one
record to the common `Posting` schema. Fetching is kept apart from parsing, so
the offline demo and the tests feed saved payloads through the exact same
parsers as live runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from ..http import PoliteHttp
from ..models import Posting


@dataclass(frozen=True)
class Payload:
    """One API response: a company board (ATS) or a search result (job board)."""

    source: str
    key: str  # board slug, or the search query that produced it
    body: Any
    company: str | None = None  # display name configured for an ATS board


@dataclass(frozen=True)
class LiveSettings:
    roles: tuple[str, ...]
    boards: dict[str, dict[str, str]]  # source -> {slug: company name}


class Source:
    name: ClassVar[str]
    kind: ClassVar[Literal["ats", "board"]]
    # Keys a raw record must have before it can be parsed (extract-stage rule).
    required_keys: ClassVar[tuple[str, ...]]
    # What the API's terms ask of consumers (shown in the README and report).
    terms: ClassVar[str] = ""

    def fetch(self, http: PoliteHttp, settings: LiveSettings) -> Iterator[Payload]:
        raise NotImplementedError

    def records(self, payload: Payload) -> list[dict[str, Any]]:
        raise NotImplementedError

    def normalize(self, record: dict[str, Any], payload: Payload) -> Posting:
        raise NotImplementedError
