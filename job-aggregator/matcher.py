import json
from concurrent.futures import ThreadPoolExecutor
from anthropic import Anthropic
from models import Job

MODEL = "claude-haiku-4-5-20251001"


def build_prompt(job: Job, targets: dict) -> str:
    titles = ", ".join(targets.get("titles", []))
    experience = targets.get("experience_years", "")
    excludes = ", ".join(targets.get("exclude_keywords", []))
    return f"""You are a job relevance filter. Evaluate this job listing.

Job:
- Title: {job.title}
- Company: {job.company}
- Location: {job.location}
- Description: {job.description[:500]}

Target criteria:
- Target roles: {titles}
- Experience: {experience} years
- Exclude if contains: {excludes}

Respond with JSON only, no other text:
{{"is_software_industry": bool, "matches_target_role": bool, "reason": "one sentence"}}"""


class Matcher:
    def __init__(self, client: Anthropic = None):
        self.client = client or Anthropic()

    def _evaluate(self, job: Job, targets: dict) -> bool:
        prompt = build_prompt(job, targets)
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            result = json.loads(text)
            return result.get("is_software_industry", False) and result.get("matches_target_role", False)
        except (json.JSONDecodeError, KeyError, IndexError):
            return False

    def filter(self, jobs: list[Job], targets: dict) -> list[Job]:
        if not jobs:
            return []
        with ThreadPoolExecutor(max_workers=10) as pool:
            keep = list(pool.map(lambda job: self._evaluate(job, targets), jobs))
        return [job for job, ok in zip(jobs, keep) if ok]
