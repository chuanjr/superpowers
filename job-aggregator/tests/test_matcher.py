import pytest
from unittest.mock import MagicMock, patch
from models import Job, make_job_id
from matcher import Matcher, build_prompt

def _job(title, company, description=""):
    return Job(
        id=make_job_id(company, title),
        title=title, company=company,
        location="Singapore", market="sg",
        url="https://x.com", description=description,
        sources=["linkedin"],
    )

def test_build_prompt_includes_job_info():
    job = _job("Backend Engineer", "Stripe", "We build payments infra")
    targets = {"titles": ["Backend Engineer"], "experience_years": "3-5", "exclude_keywords": []}
    prompt = build_prompt(job, targets)
    assert "Backend Engineer" in prompt
    assert "Stripe" in prompt
    assert "3-5" in prompt

def test_matcher_filters_passing_job(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"is_software_industry": true, "matches_target_role": true, "reason": "matches"}')]
    )
    matcher = Matcher(client=mock_client)
    job = _job("Backend Engineer", "Stripe")
    result = matcher.filter([job], targets={"titles": ["Backend Engineer"], "experience_years": "3-5", "exclude_keywords": []})
    assert len(result) == 1

def test_matcher_removes_failing_job(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"is_software_industry": false, "matches_target_role": false, "reason": "bank"}')]
    )
    matcher = Matcher(client=mock_client)
    job = _job("Backend Engineer", "Goldman Sachs")
    result = matcher.filter([job], targets={"titles": ["Backend Engineer"], "experience_years": "3-5", "exclude_keywords": []})
    assert len(result) == 0

def test_matcher_handles_malformed_json(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Sorry, I can't help with that.")]
    )
    matcher = Matcher(client=mock_client)
    job = _job("SWE", "Unknown Corp")
    # Should not raise; malformed response = exclude
    result = matcher.filter([job], targets={"titles": ["SWE"], "experience_years": "3+", "exclude_keywords": []})
    assert len(result) == 0
