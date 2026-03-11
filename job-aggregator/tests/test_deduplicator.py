from models import Job, make_job_id
from deduplicator import deduplicate, remove_seen

def _job(title, company, source, market="tw"):
    return Job(
        id=make_job_id(company, title),
        title=title, company=company,
        location="Taipei", market=market,
        url="https://x.com", description="",
        sources=[source],
    )

def test_deduplicate_merges_same_job_across_sources():
    jobs = [
        _job("Backend Engineer", "Stripe", "linkedin"),
        _job("Backend Engineer", "Stripe", "indeed"),
    ]
    result = deduplicate(jobs)
    assert len(result) == 1
    assert set(result[0].sources) == {"linkedin", "indeed"}

def test_deduplicate_keeps_different_jobs():
    jobs = [
        _job("Backend Engineer", "Stripe", "linkedin"),
        _job("Frontend Engineer", "Stripe", "linkedin"),
    ]
    result = deduplicate(jobs)
    assert len(result) == 2

def test_remove_seen_filters_known_ids():
    jobs = [
        _job("Backend Engineer", "Stripe", "linkedin"),
        _job("Frontend Engineer", "Shopify", "indeed"),
    ]
    seen = {jobs[0].id}
    result = remove_seen(jobs, seen)
    assert len(result) == 1
    assert result[0].title == "Frontend Engineer"

def test_remove_seen_empty_seen_returns_all():
    jobs = [_job("SWE", "Stripe", "linkedin")]
    assert remove_seen(jobs, set()) == jobs
