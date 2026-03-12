"""SQLite-backed job store — replaces state.json.

Cross-day deduplication is handled by the PRIMARY KEY (job.id = company+title hash).
New jobs are INSERTed; existing jobs get their last_seen_at updated.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models import Job

DB_PATH = Path("jobs.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path = DB_PATH) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            company      TEXT,
            location     TEXT,
            market       TEXT,
            url          TEXT,
            description  TEXT,
            sources      TEXT,
            industry     TEXT,
            stage        TEXT,
            posted_at    TEXT,
            fetched_at   TEXT,
            logo_url     TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def upsert_jobs(jobs: list[Job]) -> list[Job]:
    """Upsert jobs into DB. Returns only the newly inserted (first-time) jobs."""
    now = datetime.now(timezone.utc).isoformat()
    new_jobs: list[Job] = []
    with _conn() as conn:
        for job in jobs:
            existing = conn.execute(
                "SELECT id FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if existing:
                # Update sources (may have merged new platforms) and last_seen_at
                conn.execute(
                    "UPDATE jobs SET last_seen_at = ?, sources = ?, url = COALESCE(?, url) WHERE id = ?",
                    (now, json.dumps(job.sources), job.url or None, job.id),
                )
            else:
                conn.execute(
                    """INSERT INTO jobs
                       (id, title, company, location, market, url, description,
                        sources, industry, stage, posted_at, fetched_at, logo_url,
                        first_seen_at, last_seen_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job.id, job.title, job.company, job.location, job.market,
                        job.url, job.description, json.dumps(job.sources),
                        job.industry, job.stage, job.posted_at, job.fetched_at,
                        getattr(job, "logo_url", None),
                        now, now,
                    ),
                )
                new_jobs.append(job)
        conn.commit()
    return new_jobs


def get_all_jobs() -> list[dict]:
    """Return all jobs ordered newest-first (by first_seen_at)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY first_seen_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["sources"] = json.loads(d.get("sources") or "[]")
        result.append(d)
    return result


def get_existing_ids() -> set[str]:
    """Return all job IDs currently in the DB (for cross-day dedup)."""
    with _conn() as conn:
        rows = conn.execute("SELECT id FROM jobs").fetchall()
    return {r["id"] for r in rows}
