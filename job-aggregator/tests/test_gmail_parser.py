from pathlib import Path
from fetchers.gmail_parser import parse_gmail_message

def test_parse_linkedin_alert():
    html = Path("tests/fixtures/linkedin_alert.html").read_text()
    entries = parse_gmail_message(html, source="linkedin", market="sg")
    assert len(entries) == 2
    # old format: "Title at Company" in single link
    assert entries[0]["title"] == "Backend Engineer"
    assert entries[0]["company"] == "Stripe"
    assert entries[0]["source"] == "linkedin"
    assert entries[0]["market"] == "sg"
    # new format: title-only link
    assert entries[1]["title"] == "Product Manager"

def test_parse_indeed_alert():
    html = Path("tests/fixtures/indeed_alert.html").read_text()
    entries = parse_gmail_message(html, source="indeed", market="sg")
    assert len(entries) == 1
    assert "Software Engineer" in entries[0]["title"]
    assert entries[0]["company"] == "Shopify"

def test_parse_returns_empty_for_blank_html():
    entries = parse_gmail_message("", source="linkedin", market="tw")
    assert entries == []
