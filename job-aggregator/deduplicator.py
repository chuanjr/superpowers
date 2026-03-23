from models import Job, _strip_url_params


def deduplicate(jobs: list[Job]) -> list[Job]:
    seen: dict[str, Job] = {}       # job_id → Job
    url_to_id: dict[str, str] = {}  # canonical URL → job_id (cross-language dedup)

    for job in jobs:
        # Secondary dedup by URL — catches same posting under different company name
        # (e.g. 玩美移動 via CakeResume vs "Perfect Corp" via LinkedIn)
        canonical_url = _strip_url_params(job.url) if job.url else ""
        if canonical_url and canonical_url in url_to_id:
            existing_id = url_to_id[canonical_url]
            existing = seen[existing_id]
            merged_sources = list(set(existing.sources + job.sources))
            seen[existing_id] = Job(
                id=existing.id, title=existing.title, company=existing.company,
                location=existing.location, market=existing.market, url=existing.url,
                description=existing.description or job.description,
                sources=merged_sources,
                industry=existing.industry, stage=existing.stage,
                fetched_at=existing.fetched_at,
            )
            continue

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
            if canonical_url:
                url_to_id[canonical_url] = job.id

    return list(seen.values())


def remove_seen(jobs: list[Job], seen_ids: set[str]) -> list[Job]:
    return [j for j in jobs if j.id not in seen_ids]
