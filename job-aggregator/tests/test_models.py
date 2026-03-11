import hashlib
from models import Job, make_job_id

def test_make_job_id_is_deterministic():
    assert make_job_id("Stripe", "Backend Engineer") == make_job_id("Stripe", "Backend Engineer")

def test_make_job_id_differs_by_company():
    assert make_job_id("Stripe", "Backend Engineer") != make_job_id("Shopify", "Backend Engineer")

def test_job_defaults():
    job = Job(
        id="abc",
        title="Backend Engineer",
        company="Stripe",
        location="Singapore",
        market="sg",
        url="https://example.com/job/1",
        description="We are hiring...",
        sources=["linkedin"],
    )
    assert job.industry is None
    assert job.stage is None
    assert job.fetched_at == ""

def test_job_sources_merged():
    job = Job(
        id="abc", title="SWE", company="Stripe", location="SG",
        market="sg", url="https://x.com", description="",
        sources=["linkedin", "indeed"],
    )
    assert "linkedin" in job.sources
    assert "indeed" in job.sources
