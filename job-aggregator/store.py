"""SQLite-backed job store — replaces state.json.

Cross-day deduplication is handled by the PRIMARY KEY (job.id = company+title hash).
New jobs are INSERTed; existing jobs get their last_seen_at updated.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
    # Migration: add embedding column for resume matching
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN embedding TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resumes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT,
            raw_text    TEXT,
            parsed_json TEXT,
            embedding   TEXT,
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resume_job_matches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            resume_id   INTEGER NOT NULL REFERENCES resumes(id),
            job_id      TEXT NOT NULL REFERENCES jobs(id),
            similarity  REAL,
            score       INTEGER,
            explanation TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(resume_id, job_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT NOT NULL REFERENCES jobs(id),
            resume_id   INTEGER REFERENCES resumes(id),
            status      TEXT NOT NULL DEFAULT 'recommended',
            verdict     TEXT,
            notes       TEXT,
            added_at    TEXT NOT NULL,
            reviewed_at TEXT,
            UNIQUE(job_id)
        )
    """)
    # Migration: add name/headline to resumes
    try:
        conn.execute("ALTER TABLE resumes ADD COLUMN name TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE resumes ADD COLUMN headline TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT NOT NULL REFERENCES jobs(id),
            resume_id   INTEGER REFERENCES resumes(id),
            rating      TEXT NOT NULL,
            reason      TEXT,
            created_at  TEXT NOT NULL
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
    """Return all jobs ordered newest-first (by first_seen_at). Excludes embedding column."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, title, company, location, market, url, description,
                      sources, industry, stage, posted_at, fetched_at, logo_url,
                      first_seen_at, last_seen_at
               FROM jobs ORDER BY first_seen_at DESC"""
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


# ── Resume store ───────────────────────────────────────────────────────────────

def save_resume(filename: str, raw_text: str) -> int:
    """Insert a new resume record and return its ID."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO resumes (filename, raw_text, status, created_at) VALUES (?, ?, 'pending', ?)",
            (filename, raw_text, now),
        )
        conn.commit()
        return cur.lastrowid


def update_resume(resume_id: int, *,
                  parsed_json: Optional[str] = None,
                  embedding: Optional[str] = None,
                  status: Optional[str] = None) -> None:
    """Partially update a resume row."""
    updates, values = [], []
    if parsed_json is not None:
        updates.append("parsed_json = ?"); values.append(parsed_json)
    if embedding is not None:
        updates.append("embedding = ?"); values.append(embedding)
    if status is not None:
        updates.append("status = ?"); values.append(status)
    if not updates:
        return
    values.append(resume_id)
    with _conn() as conn:
        conn.execute(f"UPDATE resumes SET {', '.join(updates)} WHERE id = ?", values)
        conn.commit()


def get_resume(resume_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
    return dict(row) if row else None


def get_latest_resume() -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM resumes ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def recover_stale_resumes() -> None:
    """On server startup, mark any résumé stuck in 'processing' as done or error.

    A 'processing' status that survives a server restart means the background
    task was killed mid-run.  If matches already exist we call it 'done';
    otherwise we mark it as an error so the user knows to re-upload.
    """
    with _conn() as conn:
        stuck = conn.execute(
            "SELECT id FROM resumes WHERE status = 'processing'"
        ).fetchall()
        for row in stuck:
            rid = row["id"]
            has_matches = conn.execute(
                "SELECT 1 FROM resume_job_matches WHERE resume_id = ? LIMIT 1",
                (rid,),
            ).fetchone()
            new_status = "done" if has_matches else "error: interrupted"
            conn.execute(
                "UPDATE resumes SET status = ? WHERE id = ?", (new_status, rid)
            )
        conn.commit()


# ── Job embedding helpers ──────────────────────────────────────────────────────

def get_jobs_needing_embedding() -> list[dict]:
    """Return jobs that don't yet have an embedding stored."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, title, company, description FROM jobs WHERE embedding IS NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_jobs_with_embeddings() -> list[dict]:
    """Return id + embedding for all jobs (internal use only)."""
    with _conn() as conn:
        rows = conn.execute("SELECT id, embedding FROM jobs WHERE embedding IS NOT NULL").fetchall()
    return [dict(r) for r in rows]


def update_job_embedding(job_id: str, embedding_json: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE jobs SET embedding = ? WHERE id = ?", (embedding_json, job_id))
        conn.commit()


# ── Resume-job match helpers ───────────────────────────────────────────────────

def upsert_match(resume_id: int, job_id: str, similarity: float,
                 score: Optional[int] = None, explanation: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO resume_job_matches (resume_id, job_id, similarity, score, explanation, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(resume_id, job_id) DO UPDATE SET
                similarity  = excluded.similarity,
                score       = COALESCE(excluded.score, score),
                explanation = COALESCE(excluded.explanation, explanation)
        """, (resume_id, job_id, similarity, score, explanation, now))
        conn.commit()


def get_matches(resume_id: int) -> list[dict]:
    """Return all match rows for a resume, joined with job metadata, best first."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                rjm.job_id, rjm.similarity, rjm.score, rjm.explanation,
                j.title, j.company, j.location, j.market, j.url,
                j.logo_url, j.sources, j.first_seen_at
            FROM resume_job_matches rjm
            JOIN jobs j ON j.id = rjm.job_id
            WHERE rjm.resume_id = ?
            ORDER BY COALESCE(rjm.score, CAST(rjm.similarity * 100 AS INTEGER)) DESC
        """, (resume_id,)).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["sources"] = json.loads(d.get("sources") or "[]")
        result.append(d)
    return result


# ── Resume name/headline ───────────────────────────────────────────────────────

def update_resume_identity(resume_id: int, name: str, headline: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE resumes SET name = ?, headline = ? WHERE id = ?",
                     (name, headline, resume_id))
        conn.commit()


def get_latest_resume_identity() -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT name, headline FROM resumes WHERE name IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else {"name": "", "headline": ""}


# ── Pipeline ───────────────────────────────────────────────────────────────────

def add_to_pipeline(job_id: str, resume_id: Optional[int] = None,
                    status: str = "recommended", verdict: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO pipeline (job_id, resume_id, status, verdict, added_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                resume_id = COALESCE(excluded.resume_id, resume_id),
                status    = excluded.status,
                verdict   = COALESCE(excluded.verdict, verdict)
        """, (job_id, resume_id, status, verdict, now))
        conn.commit()


def get_pipeline() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                p.id, p.job_id, p.resume_id, p.status, p.verdict, p.notes,
                p.added_at, p.reviewed_at,
                j.title, j.company, j.location, j.market, j.url, j.logo_url, j.sources,
                rjm.score, rjm.similarity, rjm.explanation
            FROM pipeline p
            JOIN jobs j ON j.id = p.job_id
            LEFT JOIN resume_job_matches rjm ON rjm.job_id = p.job_id
                AND rjm.resume_id = p.resume_id
            ORDER BY COALESCE(rjm.score, CAST(rjm.similarity * 100 AS INTEGER)) DESC NULLS LAST
        """).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["sources"] = json.loads(d.get("sources") or "[]")
        result.append(d)
    return result


def update_pipeline_entry(job_id: str, status: Optional[str] = None,
                           verdict: Optional[str] = None,
                           notes: Optional[str] = None,
                           reviewed_at: Optional[str] = None) -> None:
    updates, values = [], []
    if status is not None:
        updates.append("status = ?"); values.append(status)
    if verdict is not None:
        updates.append("verdict = ?"); values.append(verdict)
    if notes is not None:
        updates.append("notes = ?"); values.append(notes)
    if reviewed_at is not None:
        updates.append("reviewed_at = ?"); values.append(reviewed_at)
    if not updates:
        return
    values.append(job_id)
    with _conn() as conn:
        conn.execute(f"UPDATE pipeline SET {', '.join(updates)} WHERE job_id = ?", values)
        conn.commit()


def remove_from_pipeline(job_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM pipeline WHERE job_id = ?", (job_id,))
        conn.commit()


def get_pipeline_job_ids() -> set[str]:
    with _conn() as conn:
        rows = conn.execute("SELECT job_id FROM pipeline").fetchall()
    return {r["job_id"] for r in rows}


# ── Single job lookup ──────────────────────────────────────────────────────────

def get_job(job_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            """SELECT id, title, company, location, market, url, description,
                      sources, industry, stage, logo_url
               FROM jobs WHERE id = ?""",
            (job_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["sources"] = json.loads(d.get("sources") or "[]")
    return d


# ── Feedback ───────────────────────────────────────────────────────────────────

def save_feedback(job_id: str, resume_id: Optional[int],
                  rating: str, reason: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO feedback (job_id, resume_id, rating, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, resume_id, rating, reason, now),
        )
        conn.commit()


def get_feedback_for_job(job_id: str, resume_id: Optional[int] = None) -> list[dict]:
    with _conn() as conn:
        if resume_id is not None:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE job_id = ? AND resume_id = ? ORDER BY created_at DESC",
                (job_id, resume_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE job_id = ? ORDER BY created_at DESC",
                (job_id,),
            ).fetchall()
    return [dict(r) for r in rows]
