"""Registry of the job sources. Every one of them is a public JSON API."""

from __future__ import annotations

from .ats import Ashby, Greenhouse, Lever
from .base import LiveSettings, Payload, Source
from .boards import Himalayas, Jobicy, RemoteOK, Remotive

SOURCES: dict[str, Source] = {
    source.name: source
    for source in (
        Greenhouse(),
        Lever(),
        Ashby(),
        Remotive(),
        RemoteOK(),
        Jobicy(),
        Himalayas(),
    )
}

__all__ = ["SOURCES", "LiveSettings", "Payload", "Source"]
