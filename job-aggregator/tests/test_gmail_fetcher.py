import base64
from unittest.mock import MagicMock, patch
from fetchers.gmail_fetcher import GmailFetcher, build_search_query

def test_build_search_query_linkedin():
    query = build_search_query(sources={"linkedin_gmail": True}, days_back=1)
    assert "jobs-noreply@linkedin.com" in query

def test_build_search_query_indeed():
    query = build_search_query(sources={"indeed_gmail": True}, days_back=1)
    assert "indeed.com" in query

def test_build_search_query_respects_disabled_source():
    query = build_search_query(
        sources={"linkedin_gmail": False, "indeed_gmail": True}, days_back=1
    )
    assert "linkedin" not in query

def _make_mock_message(html_body: str) -> dict:
    encoded = base64.urlsafe_b64encode(html_body.encode()).decode()
    return {
        "id": "msg123",
        "payload": {
            "mimeType": "text/html",
            "body": {"data": encoded},
        },
    }

def test_extract_raw_html():
    from fetchers.gmail_fetcher import extract_html_body
    from pathlib import Path
    html = Path("tests/fixtures/linkedin_alert.html").read_text()
    msg = _make_mock_message(html)
    assert "Backend Engineer" in extract_html_body(msg)
