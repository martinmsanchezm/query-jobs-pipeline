from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from qjpipeline.models import Posting
from qjpipeline.pipeline import sample_payloads
from qjpipeline.sources import SOURCES

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"
DAY1 = datetime(2026, 9, 29, 12, 0)
DAY2 = datetime(2026, 9, 30, 12, 0)


@pytest.fixture
def day1_payloads():
    return list(sample_payloads(SAMPLES / "day1", SAMPLES / "boards.toml"))


def normalized(source: str, day: str = "day1") -> list[Posting]:
    """Every record of one source's sample payload, normalized (no quality rules)."""
    out = []
    for payload in sample_payloads(SAMPLES / day, SAMPLES / "boards.toml"):
        if payload.source == source:
            impl = SOURCES[source]
            for record in impl.records(payload):
                if all(record.get(k) not in (None, "") for k in impl.required_keys):
                    out.append(impl.normalize(record, payload))
    return out


def posting(**overrides) -> Posting:
    base = {
        "source": "remotive",
        "source_job_id": "1",
        "url": "https://jobs.example/1",
        "title": "Data Engineer",
        "company": "Contoso Logistics",
        "work_mode": "remote",
        "posted_at": datetime(2026, 9, 28),
    }
    base.update(overrides)
    return Posting(**base)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
