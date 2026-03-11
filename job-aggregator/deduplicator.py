from models import Job


def deduplicate(jobs: list[Job]) -> list[Job]:
    seen: dict[str, Job] = {}
    for job in jobs:
        if job.id in seen:
            existing = seen[job.id]
            merged_sources = list(set(existing.sources + job.sources))
            seen[job.id] = Job(
                id=existing.id, title=existing.title, company=existing.company,
                location=existing.location, market=existing.market, url=existing.url,
                description=existing.description or job.description,
                sources=merged_sources,
                industry=existing.industry, stage=existing.stage,
                fetched_at=existing.fetched_at,
            )
        else:
            seen[job.id] = job
    return list(seen.values())


def remove_seen(jobs: list[Job], seen_ids: set[str]) -> list[Job]:
    return [j for j in jobs if j.id not in seen_ids]
