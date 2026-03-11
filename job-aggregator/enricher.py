import json
from anthropic import Anthropic
from models import Job

MODEL = "claude-haiku-4-5-20251001"


def _build_enrich_prompt(job: Job) -> str:
    return f"""Classify this company based on the job listing.

Company: {job.company}
Job title: {job.title}
Description excerpt: {job.description[:300]}

Respond with JSON only:
{{"industry": "one of: saas, marketplace, fintech, crypto, social, ecommerce, infra, healthtech, edtech, gaming, other, or null if unknown",
  "stage": "one of: pre-seed, seed, series_a, series_b, series_c, pre-ipo, public, or null if unknown"}}"""


class Enricher:
    def __init__(self, client: Anthropic = None):
        self.client = client or Anthropic()

    def _infer(self, job: Job) -> tuple[str | None, str | None]:
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=80,
                messages=[{"role": "user", "content": _build_enrich_prompt(job)}],
            )
            data = json.loads(response.content[0].text.strip())
            return data.get("industry"), data.get("stage")
        except (json.JSONDecodeError, KeyError, IndexError):
            return None, None

    def enrich(self, jobs: list[Job]) -> list[Job]:
        enriched = []
        for job in jobs:
            if "wellfound" in job.sources and job.industry is not None:
                enriched.append(job)
                continue
            industry, stage = self._infer(job)
            job.industry = industry
            job.stage = stage
            enriched.append(job)
        return enriched
