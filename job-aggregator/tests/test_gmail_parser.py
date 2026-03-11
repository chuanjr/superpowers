from pathlib import Path
from fetchers.gmail_parser import parse_gmail_message

def test_parse_linkedin_alert():
    html = Path("tests/fixtures/linkedin_alert.html").read_text()
    entries = parse_gmail_message(html, source="linkedin", market="sg")
    assert len(entries) == 1
    assert entries[0]["title"] == "Backend Engineer at Stripe"
    assert entries[0]["source"] == "linkedin"
    assert entries[0]["market"] == "sg"

def test_parse_indeed_alert():
    html = Path("tests/fixtures/indeed_alert.html").read_text()
    entries = parse_gmail_message(html, source="indeed", market="sg")
    assert len(entries) == 1
    assert "Software Engineer" in entries[0]["title"]
    assert entries[0]["company"] == "Shopify"

def test_parse_returns_empty_for_blank_html():
    entries = parse_gmail_message("", source="linkedin", market="tw")
    assert entries == []
