"""Registry of the job sources. Every one of them is a public JSON API.

Company boards on applicant tracking systems first; aggregators come next.
"""

from __future__ import annotations

from .ats import Ashby, Greenhouse, Lever
from .base import LiveSettings, Payload, Source

SOURCES: dict[str, Source] = {
    source.name: source
    for source in (
        Greenhouse(),
        Lever(),
        Ashby(),
    )
}

__all__ = ["SOURCES", "LiveSettings", "Payload", "Source"]
