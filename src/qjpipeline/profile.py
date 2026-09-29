"""The candidate profile that affinity scores are computed against.

The repository ships a FICTIONAL example (data/profile.example.json). Point
QJP_PROFILE at your own file to score against a real profile; it is never
written to the database, only its hash (to know when scores are outdated).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

LEVELS = ("junior", "mid", "senior", "lead")


@dataclass(frozen=True)
class Profile:
    name: str
    headline: str
    seniority: str
    skills: tuple[str, ...]
    min_salary_usd: int | None
    summary: str

    @property
    def fingerprint(self) -> str:
        data = json.dumps(self.as_dict(), sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "headline": self.headline,
            "seniority": self.seniority,
            "skills": list(self.skills),
            "min_salary_usd": self.min_salary_usd,
            "summary": self.summary,
        }


def load_profile(path: str | Path) -> Profile:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    seniority = data["seniority"]
    if seniority not in LEVELS:
        raise ValueError(f"profile seniority must be one of {LEVELS}, got {seniority!r}")
    return Profile(
        name=data["name"],
        headline=data["headline"],
        seniority=seniority,
        skills=tuple(data["skills"]),
        min_salary_usd=data.get("min_salary_usd"),
        summary=data.get("summary", ""),
    )
