from datetime import date
from models import Job, make_job_id
from notifier import build_email_html, build_subject

def _job(title, company, market="tw", industry="saas", stage="series_b"):
    j = Job(
        id=make_job_id(company, title),
        title=title, company=company, location="Taipei",
        market=market, url="https://example.com/job/1",
        description="", sources=["linkedin"],
    )
    j.industry = industry
    j.stage = stage
    return j

def test_build_subject_with_matches():
    jobs = [_job("SWE", "Stripe", "sg"), _job("Backend", "Shopify", "tw")]
    subject = build_subject(jobs, date(2026, 3, 11))
    assert "2026-03-11" in subject
    assert "2" in subject

def test_build_subject_no_matches():
    subject = build_subject([], date(2026, 3, 11))
    assert "No new matches" in subject

def test_build_email_html_contains_job_info():
    jobs = [_job("Backend Engineer", "Stripe", "sg", "fintech", "public")]
    html = build_email_html(jobs, date(2026, 3, 11))
    assert "Backend Engineer" in html
    assert "Stripe" in html
    assert "fintech" in html
    assert "public" in html
    assert "https://example.com/job/1" in html

def test_build_email_html_empty():
    html = build_email_html([], date(2026, 3, 11))
    assert "No new matches" in html
