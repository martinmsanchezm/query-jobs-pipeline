"""Company job boards hosted on applicant tracking systems (public JSON APIs).

    Greenhouse  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
    Lever       https://api.lever.co/v0/postings/{slug}?mode=json
    Ashby       https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

Each board belongs to one company, so the company name comes from the board
configuration rather than from the record.
"""

from __future__ import annotations

import html
from collections.abc import Iterator
from typing import Any

from ..http import PoliteHttp, SourceError
from ..models import Posting, WorkMode
from ..text import clean, html_to_text, parse_datetime, positive_int, salary_text
from .base import LiveSettings, Payload, Source

_REMOTE_HINTS = ("remote", "anywhere", "work from home", "distributed")


def work_mode_from_text(location: str | None, title: str) -> WorkMode | None:
    """Boards without a workplace field: "remote" anywhere wins; a place alone is on-site."""
    haystack = f"{location or ''} {title}".lower()
    if any(hint in haystack for hint in _REMOTE_HINTS):
        return "remote"
    if "hybrid" in haystack:
        return "hybrid"
    return "onsite" if location else None


class _AtsSource(Source):
    kind = "ats"
    url_template: str
    params: dict[str, str]

    def fetch(self, http: PoliteHttp, settings: LiveSettings) -> Iterator[Payload]:
        for slug, company in settings.boards.get(self.name, {}).items():
            try:
                body = http.get_json(self.url_template.format(slug=slug), self.params)
            except SourceError:
                continue  # one missing board must not stop the others
            yield Payload(self.name, slug, body, company)


class Greenhouse(_AtsSource):
    name = "greenhouse"
    required_keys = ("id", "title", "absolute_url")
    url_template = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    params = {"content": "true"}  # noqa: RUF012 - read-only class constant

    def records(self, payload: Payload) -> list[dict[str, Any]]:
        return list(payload.body.get("jobs", []))

    def normalize(self, record: dict[str, Any], payload: Payload) -> Posting:
        title = clean(record["title"]) or ""
        location = clean((record.get("location") or {}).get("name"))
        # Greenhouse HTML-escapes the content field a second time.
        description = html_to_text(html.unescape(record.get("content") or ""))
        return Posting(
            source=self.name,
            source_job_id=f"{payload.key}:{record['id']}",
            url=record["absolute_url"],
            title=title,
            company=payload.company or clean(record.get("company_name")) or payload.key,
            location=location,
            work_mode=work_mode_from_text(location, title),
            posted_at=parse_datetime(record.get("first_published") or record.get("updated_at")),
            description=description,
            raw={k: v for k, v in record.items() if k != "content"},
        )


_LEVER_WORKPLACE: dict[str, WorkMode] = {
    "remote": "remote",
    "hybrid": "hybrid",
    "on-site": "onsite",
    "onsite": "onsite",
}
_LEVER_PERIOD = {"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour"}


class Lever(_AtsSource):
    name = "lever"
    required_keys = ("id", "text", "hostedUrl")
    url_template = "https://api.lever.co/v0/postings/{slug}"
    params = {"mode": "json"}  # noqa: RUF012 - read-only class constant

    def records(self, payload: Payload) -> list[dict[str, Any]]:
        return list(payload.body)

    def normalize(self, record: dict[str, Any], payload: Payload) -> Posting:
        title = clean(record["text"]) or ""
        categories = record.get("categories") or {}
        location = clean(categories.get("location"))
        mode = _LEVER_WORKPLACE.get(str(record.get("workplaceType") or "").lower())
        pay = record.get("salaryRange") or {}
        period = _LEVER_PERIOD.get(pay.get("interval", ""), "year")
        low, high = positive_int(pay.get("min")), positive_int(pay.get("max"))
        currency = clean(pay.get("currency"))
        sections = [record.get("descriptionPlain")]
        for block in record.get("lists") or []:
            body = html_to_text(block.get("content"))
            sections.append("\n".join(filter(None, [clean(block.get("text")), body])))
        sections.append(record.get("additionalPlain"))
        description = "\n\n".join(s.strip() for s in sections if s and s.strip()) or None
        return Posting(
            source=self.name,
            source_job_id=f"{payload.key}:{record['id']}",
            url=record["hostedUrl"],
            title=title,
            company=payload.company or payload.key,
            location=location,
            work_mode=mode or work_mode_from_text(location, title),
            salary_min=low if period == "year" else None,
            salary_max=high if period == "year" else None,
            salary_currency=currency,
            salary_text=salary_text(low, high, currency, period),
            posted_at=parse_datetime(record.get("createdAt")),
            description=description,
            raw={k: record.get(k) for k in ("id", "text", "hostedUrl", "workplaceType")},
        )


_ASHBY_WORKPLACE: dict[str, WorkMode] = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


class Ashby(_AtsSource):
    name = "ashby"
    required_keys = ("id", "title")
    url_template = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
    params = {"includeCompensation": "true"}  # noqa: RUF012 - read-only class constant

    def records(self, payload: Payload) -> list[dict[str, Any]]:
        # Unlisted postings are reachable by link only: not part of the public board.
        return [job for job in payload.body.get("jobs", []) if job.get("isListed", True)]

    def normalize(self, record: dict[str, Any], payload: Payload) -> Posting:
        title = clean(record["title"]) or ""
        location = clean(record.get("location"))
        workplace = str(record.get("workplaceType") or "").lower().replace("-", "")
        mode = _ASHBY_WORKPLACE.get(workplace)
        if mode is None:
            mode = "remote" if record.get("isRemote") else work_mode_from_text(location, title)
        low = high = currency = None
        compensation = record.get("compensation") or {}
        for part in compensation.get("summaryComponents") or []:
            if part.get("compensationType") == "Salary" and part.get("interval") == "1 YEAR":
                low, high = positive_int(part.get("minValue")), positive_int(part.get("maxValue"))
                currency = clean(part.get("currencyCode"))
                break
        return Posting(
            source=self.name,
            source_job_id=f"{payload.key}:{record['id']}",
            url=record.get("jobUrl") or record.get("applyUrl") or "",
            title=title,
            company=payload.company or payload.key,
            location=location,
            work_mode=mode,
            salary_min=low,
            salary_max=high,
            salary_currency=currency,
            salary_text=clean(compensation.get("compensationTierSummary"))
            or salary_text(low, high, currency),
            posted_at=parse_datetime(record.get("publishedAt")),
            description=record.get("descriptionPlain")
            or html_to_text(record.get("descriptionHtml")),
            raw={k: record.get(k) for k in ("id", "title", "workplaceType", "isRemote")},
        )
