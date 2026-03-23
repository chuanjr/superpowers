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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_culture (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_text    TEXT NOT NULL,
            parsed_json TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_stories (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            primary_skill   TEXT,
            secondary_skill TEXT,
            strength        INTEGER,
            detail          TEXT,
            created_at      TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_packages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id          TEXT NOT NULL REFERENCES jobs(id),
            resume_id       INTEGER REFERENCES resumes(id),
            culture_score   INTEGER,
            culture_signals TEXT,
            job_translation TEXT,
            story_matches   TEXT,
            ats_gap         TEXT,
            ats_resume      TEXT,
            why_company     TEXT,
            value_prop      TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            created_at      TEXT NOT NULL,
            UNIQUE(job_id, resume_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS triage_summaries (
            job_id      TEXT PRIMARY KEY,
            resume_id   INTEGER,
            summary_json TEXT,
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS company_culture_cache (
            company_key  TEXT PRIMARY KEY,
            company      TEXT NOT NULL,
            snippets_json TEXT,
            parsed_json  TEXT,
            fetched_at   TEXT NOT NULL
        )
    """)
    conn.commit()
    # Migrations for columns added after initial schema
    try:
        conn.execute("ALTER TABLE candidate_culture ADD COLUMN sort_order INTEGER")
        conn.commit()
    except Exception:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE application_packages ADD COLUMN ats_resume TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE application_packages ADD COLUMN custom_resume_text TEXT")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE application_packages ADD COLUMN custom_resume_filename TEXT")
        conn.commit()
    except Exception:
        pass
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
            if not existing and job.url:
                # Cross-language URL dedup: same posting may arrive with a different
                # company name (e.g. 玩美移動 vs "Perfect Corp"). If the URL is already
                # in the DB under a different id, merge sources into the existing record.
                from models import _strip_url_params
                canonical = _strip_url_params(job.url)
                if canonical:
                    url_existing = conn.execute(
                        "SELECT id, sources FROM jobs WHERE url = ? OR url = ?",
                        (canonical, canonical + "/"),
                    ).fetchone()
                    if url_existing:
                        existing_sources = json.loads(url_existing["sources"] or "[]")
                        merged = json.dumps(list(set(existing_sources + job.sources)))
                        conn.execute(
                            "UPDATE jobs SET last_seen_at = ?, sources = ? WHERE id = ?",
                            (now, merged, url_existing["id"]),
                        )
                        continue  # Don't insert; treat as existing
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


def get_latest_done_resume() -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM resumes WHERE status = 'done' ORDER BY id DESC LIMIT 1"
        ).fetchone()
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


def update_job_description(job_id: str, description: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (description, job_id))
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
                rjm.score, rjm.similarity, rjm.explanation,
                ap.culture_score, ap.status AS pkg_status
            FROM pipeline p
            JOIN jobs j ON j.id = p.job_id
            LEFT JOIN resume_job_matches rjm ON rjm.job_id = p.job_id
                AND rjm.resume_id = p.resume_id
            LEFT JOIN application_packages ap ON ap.job_id = p.job_id
                AND ap.resume_id = p.resume_id
            WHERE p.status != 'triage'
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


# ── Candidate culture ──────────────────────────────────────────────────────────

def upsert_culture(raw_text: str, parsed_json: Optional[str] = None) -> int:
    """Insert a new culture record (supports multiple entries). Returns its id."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO candidate_culture (raw_text, parsed_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (raw_text, parsed_json, now, now),
        )
        conn.commit()
        return cur.lastrowid


def update_culture_parsed(culture_id: int, parsed_json: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE candidate_culture SET parsed_json = ?, updated_at = ? WHERE id = ?",
            (parsed_json, now, culture_id),
        )
        conn.commit()


def get_culture() -> Optional[dict]:
    """Return most recent culture record (for backwards compat)."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM candidate_culture ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_all_culture() -> list[dict]:
    """Return all culture entries ordered by sort_order then id."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM candidate_culture ORDER BY COALESCE(sort_order, id) ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_culture_raw_text_merged() -> str:
    """Concatenate all culture raw_text entries for use in AI prompts."""
    rows = get_all_culture()
    return "\n\n---\n\n".join(r["raw_text"] for r in rows)


def update_culture_entry(culture_id: int, raw_text: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE candidate_culture SET raw_text = ?, parsed_json = NULL, updated_at = ? WHERE id = ?",
            (raw_text, now, culture_id),
        )
        conn.commit()


def delete_culture_entry(culture_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM candidate_culture WHERE id = ?", (culture_id,))
        conn.commit()


def reorder_culture_entries(ordered_ids: list) -> None:
    """Set sort_order for each entry according to the given id order."""
    with _conn() as conn:
        for i, cid in enumerate(ordered_ids):
            conn.execute(
                "UPDATE candidate_culture SET sort_order = ? WHERE id = ?", (i, cid)
            )
        conn.commit()


# ── Candidate stories ──────────────────────────────────────────────────────────

def upsert_stories(stories: list[dict]) -> None:
    """Bulk upsert STAR stories."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        for s in stories:
            conn.execute("""
                INSERT INTO candidate_stories (id, title, primary_skill, secondary_skill, strength, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title           = excluded.title,
                    primary_skill   = excluded.primary_skill,
                    secondary_skill = excluded.secondary_skill,
                    strength        = excluded.strength,
                    detail          = excluded.detail
            """, (
                s.get("id"), s.get("title"), s.get("primary_skill"),
                s.get("secondary_skill"), s.get("strength"), s.get("detail"), now,
            ))
        conn.commit()


def get_stories() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM candidate_stories ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def upsert_story(story: dict) -> None:
    """Upsert a single story. Auto-generates id if not provided."""
    import time
    now = datetime.now(timezone.utc).isoformat()
    if not story.get("id"):
        # Auto-generate ID like S012
        with _conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM candidate_stories").fetchone()[0]
        story = {**story, "id": f"S{count + 1:03d}"}
    with _conn() as conn:
        conn.execute("""
            INSERT INTO candidate_stories (id, title, primary_skill, secondary_skill, strength, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title           = excluded.title,
                primary_skill   = excluded.primary_skill,
                secondary_skill = excluded.secondary_skill,
                strength        = excluded.strength,
                detail          = excluded.detail
        """, (
            story.get("id"), story.get("title"), story.get("primary_skill"),
            story.get("secondary_skill"), story.get("strength"), story.get("detail"), now,
        ))
        conn.commit()


# ── Application packages ───────────────────────────────────────────────────────

def get_application_package(job_id: str, resume_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM application_packages WHERE job_id = ? AND resume_id = ?",
            (job_id, resume_id),
        ).fetchone()
    return dict(row) if row else None


def upsert_application_package(job_id: str, resume_id: int, **fields) -> None:
    now = datetime.now(timezone.utc).isoformat()
    allowed = {"culture_score", "culture_signals", "job_translation",
               "story_matches", "ats_gap", "ats_resume", "why_company", "value_prop", "status",
               "custom_resume_text", "custom_resume_filename"}
    cols = {k: v for k, v in fields.items() if k in allowed}

    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM application_packages WHERE job_id = ? AND resume_id = ?",
            (job_id, resume_id),
        ).fetchone()
        if existing:
            if cols:
                set_clause = ", ".join(f"{k} = ?" for k in cols)
                values = list(cols.values()) + [job_id, resume_id]
                conn.execute(
                    f"UPDATE application_packages SET {set_clause} WHERE job_id = ? AND resume_id = ?",
                    values,
                )
        else:
            col_names = ["job_id", "resume_id", "status", "created_at"] + list(cols.keys())
            placeholders = ", ".join("?" for _ in col_names)
            values = [job_id, resume_id, cols.get("status", "pending"), now] + [
                cols[k] for k in cols if k != "status"
            ]
            # Rebuild properly
            col_names = ["job_id", "resume_id", "created_at"] + list(cols.keys())
            placeholders = ", ".join("?" for _ in col_names)
            values = [job_id, resume_id, now] + list(cols.values())
            conn.execute(
                f"INSERT INTO application_packages ({', '.join(col_names)}) VALUES ({placeholders})",
                values,
            )
        conn.commit()


# ── Triage ─────────────────────────────────────────────────────────────────────

def get_triage() -> list[dict]:
    """Return all pipeline entries with status 'triage', joined with job + match score."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                p.id, p.job_id, p.resume_id, p.status, p.added_at,
                j.title, j.company, j.location, j.market, j.url, j.logo_url,
                j.sources, j.description, j.first_seen_at,
                rjm.score, rjm.similarity, rjm.explanation,
                ts.summary_json, ts.status AS summary_status,
                ap.culture_score
            FROM pipeline p
            JOIN jobs j ON j.id = p.job_id
            LEFT JOIN resume_job_matches rjm ON rjm.job_id = p.job_id
                AND rjm.resume_id = p.resume_id
            LEFT JOIN triage_summaries ts ON ts.job_id = p.job_id
            LEFT JOIN application_packages ap ON ap.job_id = p.job_id
                AND ap.resume_id = p.resume_id
            WHERE p.status = 'triage'
            ORDER BY COALESCE(rjm.score, CAST(rjm.similarity * 100 AS INTEGER)) DESC NULLS LAST
        """).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["sources"] = json.loads(d.get("sources") or "[]")
        result.append(d)
    return result


def upsert_triage_summary(job_id: str, resume_id: Optional[int],
                           summary_json: str, status: str = "done") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO triage_summaries (job_id, resume_id, summary_json, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                resume_id    = excluded.resume_id,
                summary_json = excluded.summary_json,
                status       = excluded.status
        """, (job_id, resume_id, summary_json, status, now))
        conn.commit()


def get_triage_summary(job_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM triage_summaries WHERE job_id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


# ── Company culture cache ──────────────────────────────────────────────────────

def get_company_culture_cache(company_key: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM company_culture_cache WHERE company_key = ?", (company_key,)
        ).fetchone()
    return dict(row) if row else None


def upsert_company_culture_cache(company_key: str, company: str,
                                  snippets_json: str, parsed_json: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO company_culture_cache (company_key, company, snippets_json, parsed_json, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(company_key) DO UPDATE SET
                company       = excluded.company,
                snippets_json = excluded.snippets_json,
                parsed_json   = excluded.parsed_json,
                fetched_at    = excluded.fetched_at
        """, (company_key, company, snippets_json, parsed_json, now))
        conn.commit()


def list_company_culture_cache() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT company_key, company, fetched_at FROM company_culture_cache ORDER BY fetched_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Recent feedback (for scoring hints) ────────────────────────────────────────

def get_recent_feedback(resume_id: Optional[int], rating: str, limit: int = 5) -> list[dict]:
    """Return recent feedback entries for a resume, filtered by rating."""
    with _conn() as conn:
        if resume_id is not None:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE resume_id = ? AND rating = ? ORDER BY created_at DESC LIMIT ?",
                (resume_id, rating, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE rating = ? ORDER BY created_at DESC LIMIT ?",
                (rating, limit),
            ).fetchall()
    return [dict(r) for r in rows]
