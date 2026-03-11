from unittest.mock import MagicMock
from models import Job, make_job_id
from enricher import Enricher

def _job(title, company, source="linkedin"):
    return Job(
        id=make_job_id(company, title),
        title=title, company=company,
        location="SG", market="sg",
        url="https://x.com", description="We build marketplace software.",
        sources=[source],
    )

def test_enricher_wellfound_uses_native_data():
    job = _job("SWE", "Stripe", source="wellfound")
    job.industry = "fintech"
    job.stage = "public"
    enricher = Enricher(client=MagicMock())
    result = enricher.enrich([job])
    # wellfound jobs already have data; enricher should not overwrite
    assert result[0].industry == "fintech"
    assert result[0].stage == "public"

def test_enricher_infers_for_non_wellfound(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"industry": "marketplace", "stage": "series_b"}')]
    )
    enricher = Enricher(client=mock_client)
    job = _job("SWE", "Shopee", source="indeed")
    result = enricher.enrich([job])
    assert result[0].industry == "marketplace"
    assert result[0].stage == "series_b"

def test_enricher_handles_null_gracefully(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"industry": null, "stage": null}')]
    )
    enricher = Enricher(client=mock_client)
    job = _job("SWE", "Unknown Corp", source="104")
    result = enricher.enrich([job])
    assert result[0].industry is None
    assert result[0].stage is None
