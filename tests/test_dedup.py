"""Deduplication within a batch."""

from qjpipeline.dedup import canonical_url, dedup_key, group_batch, normalize_title

from .conftest import posting


def test_canonical_url_drops_tracking_and_cosmetic_differences():
    a = "https://WWW.Careers.Example/jobs/4001/?utm_source=x&gh_src=y&b=2&a=1#apply"
    b = "https://careers.example/jobs/4001?a=1&b=2"
    assert canonical_url(a) == canonical_url(b) == "https://careers.example/jobs/4001?a=1&b=2"


def test_dedup_key_ignores_legal_suffixes_and_posting_notes():
    assert dedup_key("Northwind Data, Inc.", "Senior Data Engineer (Remote - LATAM)") == dedup_key(
        "northwind data", "Senior Data Engineer"
    )
    assert normalize_title("Remote Data Engineer [US only]") == "data engineer"
    assert dedup_key(None, "Data Engineer") is None


def test_same_job_on_two_sources_is_grouped_by_url():
    ats = posting(source="lever", source_job_id="t:1", url="https://jobs.example/t/1")
    board = posting(
        source="himalayas",
        source_job_id="h1",
        url="https://jobs.example/t/1/?lever-source=himalayas",
        company="Tailspin Analytics",
        title="Data Engineer II",
    )
    (group,) = group_batch([board, ats])
    assert {link[0] for link in group.links} == {"lever", "himalayas"}
    assert group.postings[0].source == "lever"  # the ATS board is the primary record


def test_same_job_is_grouped_by_company_and_title():
    a = posting(
        source="greenhouse",
        source_job_id="n:1",
        url="https://a.example/1",
        company="Northwind Data",
        title="Senior Data Engineer",
    )
    b = posting(
        source="remotive",
        source_job_id="9",
        url="https://b.example/9",
        company="Northwind Data, Inc.",
        title="Senior Data Engineer (Remote - LATAM)",
    )
    assert len(group_batch([a, b])) == 1


def test_merge_takes_each_field_from_the_best_source_that_has_it():
    ats = posting(
        source="greenhouse",
        source_job_id="n:1",
        url="https://a.example/1",
        description="from the employer",
        salary_text=None,
    )
    board = posting(
        source="remotive",
        source_job_id="9",
        url="https://a.example/1",
        description="copied",
        salary_text="$80k - $100k",
    )
    (group,) = group_batch([board, ats])
    assert group.merged["description"] == "from the employer"
    assert group.merged["salary_text"] == "$80k - $100k"


def test_different_jobs_and_repeated_records():
    a = posting(source_job_id="1", url="https://a.example/1", title="Data Engineer")
    a_again = posting(source_job_id="1", url="https://a.example/1", title="Data Engineer")
    b = posting(source_job_id="2", url="https://a.example/2", title="Analytics Engineer")
    no_company = posting(source_job_id="3", url="https://a.example/3", company=None)
    groups = group_batch([a, a_again, b, no_company])
    assert len(groups) == 3
    assert sum(len(g.postings) for g in groups) == 3
