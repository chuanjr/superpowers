from parser import parse_raw_entry
from models import Job

def test_parse_raw_entry_rss():
    raw = {
        "title": "Backend Engineer",
        "company": "Stripe",
        "location": "Singapore",
        "market": "sg",
        "url": "https://sg.indeed.com/job/123",
        "description": "We need a backend engineer with 3+ years.",
        "source": "indeed",
    }
    job = parse_raw_entry(raw)
    assert isinstance(job, Job)
    assert job.title == "Backend Engineer"
    assert job.company == "Stripe"
    assert job.market == "sg"
    assert job.sources == ["indeed"]
    assert job.id == job.id  # deterministic

def test_parse_raw_entry_strips_whitespace():
    raw = {
        "title": "  Software Engineer  ",
        "company": " Shopify ",
        "location": "Taiwan",
        "market": "tw",
        "url": "https://example.com",
        "description": "desc",
        "source": "104",
    }
    job = parse_raw_entry(raw)
    assert job.title == "Software Engineer"
    assert job.company == "Shopify"

def test_parse_raw_entry_empty_company_is_allowed():
    """Empty company is valid — LinkedIn new-format emails don't include it."""
    raw = {
        "title": "Engineer",
        "company": "",
        "location": "", "market": "tw",
        "url": "https://x.com", "description": "", "source": "linkedin",
    }
    job = parse_raw_entry(raw)
    assert isinstance(job, Job)
    assert job.company == ""
