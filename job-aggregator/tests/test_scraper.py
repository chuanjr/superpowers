from unittest.mock import AsyncMock, patch, MagicMock
from fetchers.scraper import normalize_cakeresume_item, normalize_yourator_item

def test_normalize_cakeresume_item():
    raw = {
        "title": "Backend Engineer",
        "company": {"name": "Appier"},
        "location": "台北市",
        "url": "https://www.cakeresume.com/jobs/backend-eng",
        "description": "We are hiring a backend engineer...",
    }
    result = normalize_cakeresume_item(raw, market="tw")
    assert result["title"] == "Backend Engineer"
    assert result["company"] == "Appier"
    assert result["source"] == "cakeresume"
    assert result["market"] == "tw"

def test_normalize_yourator_item():
    raw = {
        "job_title": "Software Engineer",
        "company_name": "Appier",
        "location": "Taipei",
        "job_url": "https://www.yourator.co/companies/appier/jobs/123",
        "job_description": "We need an SWE...",
    }
    result = normalize_yourator_item(raw, market="tw")
    assert result["title"] == "Software Engineer"
    assert result["company"] == "Appier"
    assert result["source"] == "yourator"
