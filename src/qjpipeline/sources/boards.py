"""Remote job boards with public JSON APIs.

    Remotive   https://remotive.com/api/remote-jobs?search={role}
    RemoteOK   https://remoteok.com/api
    Jobicy     https://jobicy.com/api/v2/remote-jobs?tag={role}
    Himalayas  https://himalayas.app/jobs/api/search?q={role}&page={n}

All four list remote jobs only. Their terms ask for light use and for sending
people to the original posting, which is why every posting keeps its URL.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..http import PoliteHttp, SourceError
from ..models import Posting
from ..text import clean, html_to_text, parse_datetime, positive_int, salary_text
from .base import LiveSettings, Payload, Source


class _BoardSource(Source):
    kind = "board"


class Remotive(_BoardSource):
    name = "remotive"
    required_keys = ("id", "title", "url")
    terms = "Light use (a few requests per day) and a link back to Remotive."
    url = "https://remotive.com/api/remote-jobs"

    def fetch(self, http: PoliteHttp, settings: LiveSettings) -> Iterator[Payload]:
        for role in settings.roles:
            try:
                yield Payload(self.name, role, http.get_json(self.url, {"search": role}))
            except SourceError:
                continue

    def records(self, payload: Payload) -> list[dict[str, Any]]:
        return list(payload.body.get("jobs", []))

    def normalize(self, record: dict[str, Any], payload: Payload) -> Posting:
        region = clean(record.get("candidate_required_location"))
        return Posting(
            source=self.name,
            source_job_id=str(record["id"]),
            url=record["url"],
            title=clean(record["title"]) or "",
            company=clean(record.get("company_name")),
            location=region,
            work_mode="remote",
            salary_text=clean(record.get("salary")),  # free text, never parsed into numbers
            posted_at=parse_datetime(record.get("publication_date")),
            description=html_to_text(record.get("description")),
            raw={k: v for k, v in record.items() if k != "description"},
        )


class RemoteOK(_BoardSource):
    name = "remoteok"
    required_keys = ("id", "position")
    terms = "Link back to the job's RemoteOK URL and mention RemoteOK as the source."
    url = "https://remoteok.com/api"

    def fetch(self, http: PoliteHttp, settings: LiveSettings) -> Iterator[Payload]:
        try:
            yield Payload(self.name, "all", http.get_json(self.url))
        except SourceError:
            return

    def records(self, payload: Payload) -> list[dict[str, Any]]:
        # The first element is the API's legal notice, not a job.
        return [item for item in payload.body if isinstance(item, dict) and "legal" not in item]

    def normalize(self, record: dict[str, Any], payload: Payload) -> Posting:
        low, high = positive_int(record.get("salary_min")), positive_int(record.get("salary_max"))
        currency = "USD" if (low or high) else None  # RemoteOK publishes USD per year
        return Posting(
            source=self.name,
            source_job_id=str(record["id"]),
            url=record.get("url") or f"https://remoteok.com/remote-jobs/{record['id']}",
            title=clean(record["position"]) or "",
            company=clean(record.get("company")),
            location=clean(record.get("location")),
            work_mode="remote",
            salary_min=low,
            salary_max=high,
            salary_currency=currency,
            salary_text=salary_text(low, high, currency),
            posted_at=parse_datetime(record.get("epoch") or record.get("date")),
            description=html_to_text(record.get("description")),
            raw={k: v for k, v in record.items() if k != "description"},
        )


class Jobicy(_BoardSource):
    name = "jobicy"
    required_keys = ("id", "jobTitle", "url")
    terms = "Credit Jobicy and send applicants to the original job URL."
    url = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self, http: PoliteHttp, settings: LiveSettings) -> Iterator[Payload]:
        for role in settings.roles:
            try:
                yield Payload(self.name, role, http.get_json(self.url, {"count": 50, "tag": role}))
            except SourceError:
                continue

    def records(self, payload: Payload) -> list[dict[str, Any]]:
        return list(payload.body.get("jobs", []))

    def normalize(self, record: dict[str, Any], payload: Payload) -> Posting:
        low = positive_int(record.get("annualSalaryMin"))
        high = positive_int(record.get("annualSalaryMax"))
        currency = clean(record.get("salaryCurrency"))
        return Posting(
            source=self.name,
            source_job_id=str(record["id"]),
            url=record["url"],
            title=clean(record["jobTitle"]) or "",
            company=clean(record.get("companyName")),
            location=clean(record.get("jobGeo")),
            work_mode="remote",
            salary_min=low,
            salary_max=high,
            salary_currency=currency,
            salary_text=salary_text(low, high, currency),
            posted_at=parse_datetime(record.get("pubDate")),
            description=html_to_text(record.get("jobDescription") or record.get("jobExcerpt")),
            raw={k: v for k, v in record.items() if k != "jobDescription"},
        )


_HIMALAYAS_PERIOD = {"annual": "year", "yearly": "year", "monthly": "month", "hourly": "hour"}


class Himalayas(_BoardSource):
    name = "himalayas"
    required_keys = ("guid", "title")
    terms = "Light use; link to the posting on Himalayas or the employer."
    url = "https://himalayas.app/jobs/api/search"
    max_pages = 2

    def fetch(self, http: PoliteHttp, settings: LiveSettings) -> Iterator[Payload]:
        for role in settings.roles:
            for page in range(1, self.max_pages + 1):
                try:
                    body = http.get_json(self.url, {"q": role, "page": page})
                except SourceError:
                    break
                yield Payload(self.name, f"{role}#{page}", body)
                if len(body.get("jobs", [])) < body.get("limit", 20):
                    break

    def records(self, payload: Payload) -> list[dict[str, Any]]:
        return list(payload.body.get("jobs", []))

    def normalize(self, record: dict[str, Any], payload: Payload) -> Posting:
        countries = [c for c in record.get("locationRestrictions") or [] if c]
        period = _HIMALAYAS_PERIOD.get(str(record.get("salaryPeriod") or "annual").lower(), "year")
        low, high = positive_int(record.get("minSalary")), positive_int(record.get("maxSalary"))
        currency = clean(record.get("currency"))
        return Posting(
            source=self.name,
            source_job_id=str(record["guid"]),
            url=record.get("applicationLink") or record["guid"],
            title=clean(record["title"]) or "",
            company=clean(record.get("companyName")),
            location=", ".join(countries) or "Worldwide",
            work_mode="remote",
            salary_min=low if period == "year" else None,
            salary_max=high if period == "year" else None,
            salary_currency=currency,
            salary_text=salary_text(low, high, currency, period),
            posted_at=parse_datetime(record.get("pubDate")),
            description=html_to_text(record.get("description")),
            raw={k: v for k, v in record.items() if k != "description"},
        )
