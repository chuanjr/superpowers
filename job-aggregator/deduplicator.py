from models import Job, _strip_url_params


def _merge_job(existing: Job, incoming: Job) -> Job:
    """Return a copy of existing with sources merged from incoming."""
    merged_sources = list(set(existing.sources + incoming.sources))
    return Job(
        id=existing.id, title=existing.title, company=existing.company,
        location=existing.location, market=existing.market, url=existing.url,
        description=existing.description or incoming.description,
        sources=merged_sources,
        industry=existing.industry, stage=existing.stage,
        fetched_at=existing.fetched_at,
    )


def deduplicate(jobs: list[Job]) -> list[Job]:
    seen: dict[str, Job] = {}       # job_id → Job
    url_to_id: dict[str, str] = {}  # canonical URL → job_id (cross-language dedup)

    for job in jobs:
        # Secondary dedup by URL — catches same posting under different company name
        # (e.g. 玩美移動 via CakeResume vs "Perfect Corp" via LinkedIn)
        canonical_url = _strip_url_params(job.url)  # returns "" for falsy/empty URLs
        if canonical_url and canonical_url in url_to_id:
            existing_id = url_to_id[canonical_url]
            seen[existing_id] = _merge_job(seen[existing_id], job)
            continue

        if job.id in seen:
            seen[job.id] = _merge_job(seen[job.id], job)
        else:
            seen[job.id] = job
            if canonical_url:
                url_to_id[canonical_url] = job.id

    return list(seen.values())


def remove_seen(jobs: list[Job], seen_ids: set[str]) -> list[Job]:
    return [j for j in jobs if j.id not in seen_ids]
