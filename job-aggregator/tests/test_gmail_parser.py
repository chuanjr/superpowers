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


def test_duplicate_url_keeps_first_as_title():
    """Same /jobs/view/ URL appearing twice should not create duplicate entries.
    First text node is the job title; second is treated as company."""
    html = """<html><body>
      <a href="https://www.linkedin.com/comm/jobs/view/789?trk=title">Growth Product Manager</a>
      <a href="https://www.linkedin.com/comm/jobs/view/789?trk=company">Shopee</a>
    </body></html>"""
    entries = parse_gmail_message(html, source="linkedin", market="tw")
    assert len(entries) == 1
    assert entries[0]["title"] == "Growth Product Manager"
    assert entries[0]["company"] == "Shopee"


def test_tracking_params_stripped_for_dedup():
    """Two links to the same job with different tracking params → one entry."""
    html = """<html><body>
      <a href="https://www.linkedin.com/comm/jobs/view/999?trk=abc">Senior PM</a>
      <a href="https://www.linkedin.com/comm/jobs/view/999?trk=xyz">Senior PM</a>
    </body></html>"""
    entries = parse_gmail_message(html, source="linkedin", market="tw")
    assert len(entries) == 1
