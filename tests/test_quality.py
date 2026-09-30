"""Data quality rules and the relevance filter."""

from datetime import datetime

import pytest

from qjpipeline.quality import check_posting, check_record
from qjpipeline.relevance import filter_reason, title_matches

from .conftest import posting

NOW = datetime(2026, 9, 29, 12, 0)


def rule(p):
    rejection = check_posting(p, NOW)
    return None if rejection is None else rejection.rule


def test_a_valid_posting_passes():
    assert rule(posting()) is None


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ([1, 2], "not_an_object"),
        ({"id": 1, "title": "Data Engineer", "url": ""}, "missing_raw_field"),
        ({"id": 1, "title": "Data Engineer"}, "missing_raw_field"),
    ],
)
def test_extract_stage_rejects_malformed_records(record, expected):
    rejection = check_record("remotive", record, ("id", "title", "url"))
    assert rejection is not None and rejection.rule == expected and rejection.stage == "extract"


def test_extract_stage_names_the_missing_keys():
    rejection = check_record("remotive", {"id": 7, "title": ""}, ("id", "title", "url"))
    assert rejection.detail == "title, url"
    assert rejection.source_job_id == "7"


@pytest.mark.parametrize("field", ["title", "company", "url", "source_job_id"])
def test_required_fields(field):
    assert rule(posting(**{field: ""})) == "missing_required_field"


@pytest.mark.parametrize(
    "url", ["javascript:void(0)", "ftp://files.example/x", "https://localhost", "https://a b.com"]
)
def test_invalid_urls(url):
    assert rule(posting(url=url)) == "invalid_url"


@pytest.mark.parametrize(
    "posted", [datetime(1999, 12, 31), datetime(2026, 10, 1), datetime(2027, 3, 1)]
)
def test_dates_must_be_plausible(posted):
    assert rule(posting(posted_at=posted)) == "invalid_date"


def test_a_missing_date_is_allowed_and_one_day_of_skew_is_tolerated():
    assert rule(posting(posted_at=None)) is None
    assert rule(posting(posted_at=datetime(2026, 9, 30, 6, 0))) is None


@pytest.mark.parametrize(
    "salary",
    [
        {"salary_min": 130000, "salary_max": 110000},
        {"salary_min": 30},
        {"salary_max": 5_000_000},
        {"salary_min": 90000, "salary_currency": "usd"},
    ],
)
def test_salary_must_be_coherent(salary):
    assert rule(posting(**salary)) == "invalid_salary"


def test_relevance_filter():
    assert filter_reason(posting()) is None
    assert filter_reason(posting(work_mode="hybrid")).startswith("not_remote")
    assert filter_reason(posting(work_mode=None)).startswith("not_remote")
    assert filter_reason(posting(title="Product Designer")) == "other_role"
    assert title_matches("Senior Data Engineering Lead", "Data Engineer")
    assert title_matches("Ingeniero de Datos / Data Engineer (Remoto)", "Data Engineer")
    assert not title_matches("Data Analyst", "Data Engineer")
