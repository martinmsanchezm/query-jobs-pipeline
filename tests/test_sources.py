"""Normalization of each source into the common schema."""

from datetime import datetime

from qjpipeline.text import html_to_text, parse_datetime, salary_text

from .conftest import normalized


def by_id(postings, suffix):
    return next(p for p in postings if p.source_job_id.endswith(suffix))


def test_greenhouse_uses_board_company_and_unescapes_content():
    jobs = normalized("greenhouse")
    senior = by_id(jobs, ":4001")
    assert senior.company == "Northwind Data"  # from boards.toml, not the record
    assert senior.source_job_id == "northwind:4001"
    assert senior.work_mode == "remote"
    assert senior.posted_at == datetime(2026, 9, 25, 18, 0)  # -04:00 converted to UTC
    assert "- Airflow and dbt in production" in senior.description
    assert "<" not in senior.description
    assert by_id(jobs, ":4003").work_mode == "onsite"
    assert by_id(jobs, ":4004").work_mode == "hybrid"


def test_lever_reads_workplace_salary_and_lists():
    jobs = normalized("lever")
    de2 = next(p for p in jobs if p.title == "Data Engineer II")
    assert de2.work_mode == "remote"
    assert (de2.salary_min, de2.salary_max, de2.salary_currency) == (95000, 125000, "USD")
    assert de2.salary_text == "USD 95,000-125,000 / year"
    assert de2.posted_at == datetime(2026, 9, 26, 13, 0)  # epoch milliseconds
    assert "Requirements" in de2.description and "Kafka" in de2.description


def test_ashby_skips_unlisted_postings_and_reads_yearly_compensation():
    jobs = normalized("ashby")
    assert {p.title for p in jobs} == {"Senior Data Engineer", "Junior Data Engineer"}
    senior = next(p for p in jobs if p.title == "Senior Data Engineer")
    assert (senior.salary_min, senior.salary_max) == (150000, 180000)
    assert senior.salary_text == "$150K - $180K"
    assert senior.url.endswith("/ps-1001")


def test_remotive_keeps_free_text_salary_out_of_numeric_columns():
    contoso = next(p for p in normalized("remotive") if p.company == "Contoso Logistics")
    assert contoso.salary_text == "$80k - $100k"
    assert contoso.salary_min is None and contoso.salary_max is None
    assert contoso.work_mode == "remote"


def test_remoteok_skips_the_legal_notice_and_assumes_usd():
    jobs = normalized("remoteok")
    assert all(p.source_job_id != "legal" for p in jobs)
    wingtip = next(p for p in jobs if p.company == "Wingtip Data")
    assert wingtip.salary_currency == "USD"
    assert wingtip.posted_at == datetime(2026, 9, 27, 10, 46, 40)


def test_jobicy_parses_string_salaries():
    etl = next(p for p in normalized("jobicy") if p.title == "ETL Developer")
    assert (etl.salary_min, etl.salary_max, etl.salary_currency) == (70000, 85000, "USD")


def test_himalayas_prefers_the_application_link():
    jobs = normalized("himalayas")
    tailspin = next(p for p in jobs if p.company == "Tailspin Analytics")
    assert "jobs.tailspin.example" in tailspin.url
    assert tailspin.location == "United States, Canada"


def test_text_helpers():
    assert (
        html_to_text("<p>Hi&nbsp;there</p><ul><li>a</li><li>b</li></ul>") == "Hi there\n\n- a\n- b"
    )
    assert html_to_text("<p>cut <b") == "cut"
    assert parse_datetime("2026-09-25T14:00:00-04:00") == datetime(2026, 9, 25, 18, 0)
    assert parse_datetime(1758891600000) == parse_datetime(1758891600)
    assert parse_datetime("Fri, 26 Sep 2026 10:00:00 GMT") == datetime(2026, 9, 26, 10, 0)
    assert parse_datetime("not a date") is None
    assert salary_text(None, None, "USD") is None
    assert salary_text(5000, 5000, "USD", "month") == "USD 5,000 / month"
