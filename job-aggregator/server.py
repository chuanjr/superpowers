#!/usr/bin/env python3
"""Local web server for the job board UI.

Usage:
  python server.py            # starts at http://localhost:8000
  python server.py --port 9000
"""
import asyncio
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from store import (
    init_db, recover_stale_resumes, get_all_jobs, save_resume, get_resume,
    get_latest_resume, get_matches,
    add_to_pipeline, get_pipeline, update_pipeline_entry, remove_from_pipeline,
    get_pipeline_job_ids, get_latest_resume_identity,
    get_job, save_feedback,
)

app = FastAPI(title="Job Board")

_STATIC = Path(__file__).parent / "static"
_ROOT   = Path(__file__).parent


@app.get("/api/jobs")
def api_jobs() -> JSONResponse:
    """Return all jobs ordered newest-first. Filtering is done client-side."""
    return JSONResponse(get_all_jobs())


@app.get("/api/stats")
def api_stats() -> dict:
    jobs = get_all_jobs()
    sources: set[str] = set()
    markets: set[str] = set()
    for j in jobs:
        markets.add(j.get("market") or "")
        for s in j.get("sources") or []:
            sources.add(s)
    return {
        "total": len(jobs),
        "markets": sorted(m for m in markets if m),
        "sources": sorted(s for s in sources if s),
    }


# ── Resume match endpoints ─────────────────────────────────────────────────────

@app.post("/api/resume/upload")
async def upload_resume(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """Accept a PDF résumé, kick off background matching, return resume_id immediately."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    from resume_matcher import extract_pdf_text, process_matching

    pdf_bytes = await file.read()
    try:
        raw_text = extract_pdf_text(pdf_bytes)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not extract text from PDF: {exc}")

    resume_id = save_resume(filename=file.filename, raw_text=raw_text)
    background_tasks.add_task(process_matching, resume_id, raw_text)

    return {"resume_id": resume_id, "status": "processing"}


@app.post("/api/jobs/{job_id}/match")
async def match_single_job(job_id: str, background_tasks: BackgroundTasks) -> JSONResponse:
    """Run match scoring for one specific job against the latest resume."""
    resume = get_latest_resume()
    if not resume:
        raise HTTPException(status_code=404, detail="No resume found — upload one first")
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    from resume_matcher import process_single_job_match
    background_tasks.add_task(process_single_job_match, resume["id"], job_id)
    return JSONResponse({"ok": True, "resume_id": resume["id"]})


@app.post("/api/resume/rematch")
async def rematch_resume(background_tasks: BackgroundTasks) -> JSONResponse:
    """Re-run match scoring for the latest uploaded resume."""
    resume = get_latest_resume()
    if not resume:
        raise HTTPException(status_code=404, detail="No resume found — upload one first")
    from resume_matcher import process_matching
    rid      = resume["id"]
    raw_text = resume.get("raw_text", "")
    background_tasks.add_task(process_matching, rid, raw_text)
    return {"resume_id": rid, "status": "processing"}


@app.get("/api/resume/matches")
def resume_matches(resume_id: int | None = None) -> JSONResponse:
    """Return match results for a résumé (defaults to the latest uploaded)."""
    if resume_id is not None:
        resume = get_resume(resume_id)
    else:
        resume = get_latest_resume()

    if not resume:
        return JSONResponse({"status": "none", "matches": []})

    rid = resume["id"]
    matches = get_matches(rid)
    return JSONResponse({
        "resume_id": rid,
        "filename": resume.get("filename"),
        "status": resume.get("status", "pending"),
        "matches": matches,
    })


# ── Pipeline endpoints ─────────────────────────────────────────────────────────

@app.post("/api/pipeline")
def pipeline_add(body: dict) -> JSONResponse:
    job_id = body.get("job_id")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id required")
    add_to_pipeline(
        job_id=job_id,
        resume_id=body.get("resume_id"),
        status=body.get("status", "recommended"),
        verdict=body.get("verdict"),
    )
    return JSONResponse({"ok": True})


@app.get("/api/pipeline")
def pipeline_list() -> JSONResponse:
    entries = get_pipeline()
    identity = get_latest_resume_identity()
    return JSONResponse({"entries": entries, "identity": identity})


@app.get("/api/pipeline/ids")
def pipeline_ids() -> JSONResponse:
    return JSONResponse({"ids": list(get_pipeline_job_ids())})


@app.patch("/api/pipeline/{job_id}")
async def pipeline_update(job_id: str, body: dict) -> JSONResponse:
    update_pipeline_entry(
        job_id=job_id,
        status=body.get("status"),
        verdict=body.get("verdict"),
        notes=body.get("notes"),
        reviewed_at=body.get("reviewed_at"),
    )
    return JSONResponse({"ok": True})


@app.delete("/api/pipeline/{job_id}")
def pipeline_remove(job_id: str) -> JSONResponse:
    remove_from_pipeline(job_id)
    return JSONResponse({"ok": True})


# ── Refresh jobs endpoint ──────────────────────────────────────────────────────

_refresh_state: dict = {"running": False, "new_count": None, "error": None}


@app.post("/api/jobs/refresh")
def jobs_refresh() -> JSONResponse:
    if _refresh_state["running"]:
        return JSONResponse({"status": "already_running"})

    def _run() -> None:
        _refresh_state["running"] = True
        _refresh_state["error"] = None
        _refresh_state["new_count"] = None
        from store import get_existing_ids as _get_ids
        before_ids = _get_ids()
        try:
            subprocess.run(
                [sys.executable, str(_ROOT / "main.py")],
                cwd=str(_ROOT),
                timeout=300,
            )
            after_ids = _get_ids()
            new_ids = after_ids - before_ids
            _refresh_state["new_count"] = len(new_ids)

            # Auto-score new jobs against the latest resume
            if new_ids:
                resume = get_latest_resume()
                if resume and resume.get("status") == "done":
                    from resume_matcher import process_single_job_match
                    for job_id in new_ids:
                        try:
                            asyncio.run(process_single_job_match(resume["id"], job_id))
                        except Exception:
                            pass  # Don't fail the whole refresh if one job fails
        except Exception as exc:
            _refresh_state["error"] = str(exc)
        finally:
            _refresh_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"status": "started"})


@app.get("/api/jobs/refresh/status")
def jobs_refresh_status() -> JSONResponse:
    return JSONResponse({
        "running": _refresh_state["running"],
        "new_count": _refresh_state["new_count"],
        "error": _refresh_state["error"],
    })


# ── URL import endpoints ───────────────────────────────────────────────────────

@app.post("/api/jobs/import")
async def jobs_import_url(body: dict) -> JSONResponse:
    """Fetch a job posting URL and parse it into structured fields using Claude."""
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")

    import httpx
    import json as _json
    import anthropic as _anthropic

    # Fetch the page
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=headers)
        html = resp.text
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch URL: {exc}")

    # Strip HTML tags to plain text
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()[:6000]

    # Claude parses the page
    ai = _anthropic.Anthropic()
    msg = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Extract job posting information from this page. Return ONLY valid JSON with these fields:
{{
  "title": "<job title>",
  "company": "<company name>",
  "location": "<city or remote, or null>",
  "market": "<tw|jp|sg|us|null — country code based on location/company>",
  "description": "<full job description in original language, max 1500 chars>"
}}

URL: {url}

Page text:
{text}""",
        }],
    )
    raw = msg.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        parsed = _json.loads(raw.strip())
    except Exception:
        raise HTTPException(422, "Could not parse job data from page")

    parsed["url"] = url
    return JSONResponse({"job": parsed})


@app.post("/api/jobs/import/confirm")
async def jobs_import_confirm(background_tasks: BackgroundTasks, body: dict) -> JSONResponse:
    """Save a manually confirmed imported job and trigger match scoring."""
    from datetime import datetime, timezone
    from models import Job, make_job_id
    from store import upsert_jobs

    title   = (body.get("title") or "").strip()
    company = (body.get("company") or "").strip()
    if not title or not company:
        raise HTTPException(400, "title and company required")

    job_id = make_job_id(company, title, body.get("url") or "")
    now_iso = datetime.now(timezone.utc).isoformat()

    job = Job(
        id=job_id,
        title=title,
        company=company,
        location=body.get("location") or "",
        market=body.get("market") or "",
        url=body.get("url") or "",
        description=body.get("description") or "",
        sources=["manual"],
        fetched_at=now_iso,
    )
    upsert_jobs([job])

    # Trigger embedding + match for this job if we have a resume
    resume = get_latest_resume()
    if resume and resume.get("status") == "done":
        from resume_matcher import process_single_job_match
        background_tasks.add_task(process_single_job_match, resume["id"], job_id)

    return JSONResponse({"job_id": job_id, "status": "saved"})


# ── Feedback endpoint ──────────────────────────────────────────────────────────

@app.post("/api/feedback")
async def submit_feedback(background_tasks: BackgroundTasks, body: dict) -> JSONResponse:
    """Store user feedback and re-score the job for the current resume."""
    job_id  = (body.get("job_id") or "").strip()
    rating  = (body.get("rating") or "").strip()   # "up" or "down"
    reason  = body.get("reason")
    resume_id = body.get("resume_id")

    if not job_id or rating not in ("up", "down"):
        raise HTTPException(400, "job_id and rating (up|down) required")

    # Resolve resume_id if not provided
    if not resume_id:
        resume = get_latest_resume()
        if resume:
            resume_id = resume["id"]

    save_feedback(job_id, resume_id, rating, reason)

    # Re-score in background if we have a resume
    if resume_id:
        from resume_matcher import rescore_with_feedback
        background_tasks.add_task(rescore_with_feedback, resume_id, job_id, rating, reason)

    return JSONResponse({"ok": True})


# ── Candidate culture endpoints ────────────────────────────────────────────────

@app.post("/api/candidate/culture")
async def save_culture(background_tasks: BackgroundTasks, body: dict) -> JSONResponse:
    """Save culture discussion text. Claude parses it in the background."""
    raw_text = (body.get("raw_text") or "").strip()
    if not raw_text:
        raise HTTPException(400, "raw_text required")

    from store import upsert_culture, update_culture_parsed
    from application_generator import parse_culture_sync

    culture_id = upsert_culture(raw_text)

    async def _parse():
        try:
            parsed = await asyncio.to_thread(parse_culture_sync, raw_text)
            update_culture_parsed(culture_id, json.dumps(parsed))
        except Exception as exc:
            update_culture_parsed(culture_id, json.dumps({"error": str(exc)}))

    import asyncio, json
    background_tasks.add_task(_parse)
    return JSONResponse({"ok": True, "culture_id": culture_id})


@app.get("/api/candidate/culture")
def get_culture_endpoint() -> JSONResponse:
    from store import get_all_culture
    import json
    rows = get_all_culture()
    if not rows:
        return JSONResponse({"status": "none", "entries": []})
    entries = []
    for row in rows:
        parsed = {}
        if row.get("parsed_json"):
            try:
                parsed = json.loads(row["parsed_json"])
            except Exception:
                pass
        entries.append({
            "culture_id": row["id"],
            "raw_text": row["raw_text"],
            "parsed": parsed,
            "updated_at": row.get("updated_at"),
        })
    return JSONResponse({
        "status": "ok",
        "entries": entries,
        "count": len(entries),
    })


# ── Candidate stories endpoints ────────────────────────────────────────────────

@app.post("/api/candidate/stories")
def save_stories(body: dict) -> JSONResponse:
    """Bulk import STAR stories."""
    stories = body.get("stories") or []
    if not stories:
        raise HTTPException(400, "stories array required")
    from store import upsert_stories
    upsert_stories(stories)
    return JSONResponse({"ok": True, "count": len(stories)})


@app.get("/api/candidate/stories")
def list_stories() -> JSONResponse:
    from store import get_stories
    return JSONResponse({"stories": get_stories()})


# ── Application package endpoints ───────────────────────────────────────────────

_pkg_tasks: dict = {}  # job_id -> {"running": bool, "error": str|None}


@app.get("/api/pipeline/{job_id}/package")
def get_package(job_id: str) -> JSONResponse:
    import json
    resume = get_latest_resume()
    if not resume:
        return JSONResponse({"status": "no_resume"})
    from store import get_application_package
    pkg = get_application_package(job_id, resume["id"])
    if not pkg:
        task_state = _pkg_tasks.get(job_id, {})
        if task_state.get("running"):
            return JSONResponse({"status": "processing"})
        if task_state.get("error"):
            return JSONResponse({"status": "error", "error": task_state["error"]})
        return JSONResponse({"status": "none"})

    def _parse(key):
        val = pkg.get(key)
        if val and isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return val
        return val

    return JSONResponse({
        "status":          pkg.get("status", "done"),
        "job_translation": pkg.get("job_translation"),
        "culture_score":   pkg.get("culture_score"),
        "culture_signals": _parse("culture_signals"),
        "story_matches":   _parse("story_matches"),
        "ats_gap":         _parse("ats_gap"),
        "why_company":     pkg.get("why_company"),
        "value_prop":      pkg.get("value_prop"),
        "created_at":      pkg.get("created_at"),
    })


@app.post("/api/pipeline/{job_id}/package")
async def generate_package_endpoint(job_id: str, background_tasks: BackgroundTasks) -> JSONResponse:
    """Trigger application package generation for a pipeline job."""
    resume = get_latest_resume()
    if not resume:
        raise HTTPException(404, "No resume found")
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if _pkg_tasks.get(job_id, {}).get("running"):
        return JSONResponse({"status": "already_running"})

    _pkg_tasks[job_id] = {"running": True, "error": None}

    async def _run():
        import asyncio
        from application_generator import generate_package
        try:
            await asyncio.wait_for(
                generate_package(job_id, resume["id"], job, resume),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            _pkg_tasks[job_id]["error"] = "Generation timed out after 2 minutes. Please try again."
        except Exception as exc:
            _pkg_tasks[job_id]["error"] = str(exc)
        finally:
            _pkg_tasks[job_id]["running"] = False

    background_tasks.add_task(_run)
    return JSONResponse({"status": "started"})


@app.get("/api/pipeline/{job_id}/package.txt")
def download_package(job_id: str):
    import json
    from fastapi.responses import PlainTextResponse
    resume = get_latest_resume()
    if not resume:
        raise HTTPException(404, "No resume found")
    from store import get_application_package
    pkg = get_application_package(job_id, resume["id"])
    if not pkg:
        raise HTTPException(404, "Package not generated yet")

    job = get_job(job_id)
    job_title = (job or {}).get("title", "")
    company   = (job or {}).get("company", "")

    def _parse(key):
        val = pkg.get(key)
        if val and isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return val
        return val

    lines = [
        f"APPLICATION PACKAGE — {job_title} at {company}",
        "=" * 60,
        "",
    ]

    if pkg.get("job_translation"):
        lines += ["## WHAT THIS ROLE ACTUALLY IS", pkg["job_translation"], ""]

    culture_signals = _parse("culture_signals")
    if culture_signals:
        score = pkg.get("culture_score") or culture_signals.get("score", "?")
        verdict = culture_signals.get("verdict", "")
        lines += [f"## CULTURE FIT  [{score}/100]", verdict]
        if culture_signals.get("green"):
            lines += ["Green signals: " + " | ".join(culture_signals["green"])]
        if culture_signals.get("red"):
            lines += ["Red signals: " + " | ".join(culture_signals["red"])]
        lines += [""]

    story_matches = _parse("story_matches")
    if story_matches:
        lines += ["## TAILORED RESUME BULLETS"]
        for s in story_matches:
            lines += [f"[{s.get('story_id','')}] {s.get('competency','')}", f"• {s.get('bullet','')}", ""]

    ats_gap = _parse("ats_gap")
    if ats_gap:
        score = ats_gap.get("score", "?")
        lines += [f"## ATS KEYWORDS  [{score}% coverage]"]
        if ats_gap.get("present"):
            lines += ["Present: " + ", ".join(ats_gap["present"])]
        if ats_gap.get("missing"):
            lines += ["Missing (add to resume): " + ", ".join(ats_gap["missing"])]
        lines += [""]

    if pkg.get("why_company"):
        lines += ["## WHY THIS COMPANY", pkg["why_company"], ""]

    if pkg.get("value_prop"):
        lines += ["## VALUE PROPOSITION / COVER LETTER", pkg["value_prop"], ""]

    content = "\n".join(lines)
    filename = f"{company}_{job_title}_package.txt".replace(" ", "_").replace("/", "-")
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Career coach endpoints ────────────────────────────────────────────────────

@app.post("/api/coach/chat")
async def coach_chat(body: dict) -> JSONResponse:
    """General career coaching conversation (story management, career advice)."""
    import json as _json
    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(400, "messages required")

    from store import get_stories, get_all_culture, upsert_story
    from coach import chat

    stories = get_stories()
    culture_rows = get_all_culture()

    # Build merged culture DNA
    culture_dna = None
    for row in culture_rows:
        if row.get("parsed_json"):
            try:
                d = _json.loads(row["parsed_json"])
                if d and not d.get("error"):
                    if culture_dna is None:
                        culture_dna = {"likes": [], "dislikes": [], "green_signals": [], "red_signals": []}
                    for key in ("likes", "dislikes", "green_signals", "red_signals"):
                        culture_dna[key] = list(dict.fromkeys(
                            culture_dna.get(key, []) + d.get(key, [])
                        ))
                    if d.get("summary"):
                        culture_dna["summary"] = d["summary"]
            except Exception:
                pass

    resume = get_latest_resume()
    resume_summary = ""
    if resume and resume.get("parsed_json"):
        try:
            parsed = _json.loads(resume["parsed_json"])
            resume_summary = parsed.get("summary") or ""
        except Exception:
            pass

    text, story_to_save = await asyncio.to_thread(
        chat, messages, stories, culture_dna, resume_summary
    )

    saved_story_id = None
    if story_to_save:
        upsert_story(story_to_save)
        saved_story_id = story_to_save.get("id")

    return JSONResponse({
        "message": text,
        "saved_story_id": saved_story_id,
    })


@app.post("/api/coach/chat/{job_id}")
async def coach_chat_job(job_id: str, body: dict) -> JSONResponse:
    """Job-specific coaching conversation (decode JD, prep questions, match stories)."""
    import json as _json
    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(400, "messages required")

    from store import get_stories, get_all_culture, get_application_package, upsert_story
    from coach import chat

    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    stories = get_stories()
    culture_rows = get_all_culture()

    culture_dna = None
    for row in culture_rows:
        if row.get("parsed_json"):
            try:
                d = _json.loads(row["parsed_json"])
                if d and not d.get("error"):
                    if culture_dna is None:
                        culture_dna = {"likes": [], "dislikes": [], "green_signals": [], "red_signals": []}
                    for key in ("likes", "dislikes", "green_signals", "red_signals"):
                        culture_dna[key] = list(dict.fromkeys(
                            culture_dna.get(key, []) + d.get(key, [])
                        ))
                    if d.get("summary"):
                        culture_dna["summary"] = d["summary"]
            except Exception:
                pass

    resume = get_latest_resume()
    resume_summary = ""
    culture_score = None
    culture_signals = None
    if resume:
        if resume.get("parsed_json"):
            try:
                parsed = _json.loads(resume["parsed_json"])
                resume_summary = parsed.get("summary") or ""
            except Exception:
                pass
        pkg = get_application_package(job_id, resume["id"])
        if pkg:
            culture_score = pkg.get("culture_score")
            if pkg.get("culture_signals"):
                try:
                    culture_signals = _json.loads(pkg["culture_signals"])
                except Exception:
                    pass

    text, story_to_save = await asyncio.to_thread(
        chat, messages, stories, culture_dna, resume_summary,
        job, culture_score, culture_signals
    )

    saved_story_id = None
    if story_to_save:
        upsert_story(story_to_save)
        saved_story_id = story_to_save.get("id")

    return JSONResponse({
        "message": text,
        "saved_story_id": saved_story_id,
    })


@app.post("/api/candidate/stories/import")
async def import_stories_from_file(body: dict) -> JSONResponse:
    """Import stories from a coaching_state.md file path."""
    file_path = (body.get("file_path") or "").strip()
    if not file_path:
        raise HTTPException(400, "file_path required")

    from coach import import_coaching_state
    from store import upsert_stories

    stories = await asyncio.to_thread(import_coaching_state, file_path)
    upsert_stories(stories)
    return JSONResponse({"ok": True, "count": len(stories), "stories": stories})


# ── Review queue (triage) endpoints ──────────────────────────────────────────

def _build_culture_dna_from_rows(culture_rows: list) -> dict | None:
    """Merge all culture entries into a single DNA dict."""
    import json as _json
    merged: dict | None = None
    for row in culture_rows:
        if row.get("parsed_json"):
            try:
                d = _json.loads(row["parsed_json"])
                if d and not d.get("error"):
                    if merged is None:
                        merged = {"likes": [], "dislikes": [], "green_signals": [], "red_signals": []}
                    for key in ("likes", "dislikes", "green_signals", "red_signals"):
                        merged[key] = list(dict.fromkeys(merged.get(key, []) + d.get(key, [])))
                    if d.get("summary"):
                        merged["summary"] = d["summary"]
            except Exception:
                pass
    return merged


@app.get("/api/review")
def review_list() -> JSONResponse:
    """Return all jobs in the triage queue, ordered by match score."""
    import json as _json
    from store import get_triage
    entries = get_triage()
    # Strip description to keep response small
    for e in entries:
        e.pop("description", None)
    return JSONResponse({"entries": entries})


_summary_tasks: dict = {}  # job_id -> {"running": bool}


@app.post("/api/review/{job_id}/summary")
async def generate_review_summary(job_id: str, background_tasks: BackgroundTasks) -> JSONResponse:
    """Generate and cache a triage coach brief for a job."""
    import json as _json
    from store import get_triage_summary, upsert_triage_summary, get_all_culture

    # Return cached if available
    cached = get_triage_summary(job_id)
    if cached and cached.get("status") == "done":
        return JSONResponse({"status": "done", "summary": _json.loads(cached["summary_json"] or "null")})

    if _summary_tasks.get(job_id, {}).get("running"):
        return JSONResponse({"status": "processing"})

    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    resume = get_latest_resume()
    if not resume:
        raise HTTPException(404, "No resume found")

    _summary_tasks[job_id] = {"running": True}
    upsert_triage_summary(job_id, resume["id"], "{}", "pending")

    async def _run():
        from application_generator import generate_triage_summary_sync
        from store import get_all_culture, get_triage_summary as _get, upsert_triage_summary as _upsert
        try:
            parsed = _json.loads(resume.get("parsed_json") or "{}")
            resume_summary = parsed.get("summary") or ""

            # Get existing match explanation if available
            from store import get_matches
            matches = get_matches(resume["id"])
            explanation = next((m["explanation"] for m in matches if m["job_id"] == job_id), None)

            culture_rows = get_all_culture()
            culture_dna = _build_culture_dna_from_rows(culture_rows)

            summary = await asyncio.to_thread(
                generate_triage_summary_sync,
                resume_summary, job.get("description") or "",
                job.get("title") or "", job.get("company") or "",
                culture_dna, explanation,
            )
            _upsert(job_id, resume["id"], _json.dumps(summary), "done")
        except Exception as exc:
            _upsert(job_id, resume["id"], _json.dumps({"error": str(exc)}), "error")
        finally:
            _summary_tasks[job_id]["running"] = False

    background_tasks.add_task(_run)
    return JSONResponse({"status": "started"})


@app.get("/api/review/{job_id}/summary")
def get_review_summary(job_id: str) -> JSONResponse:
    import json as _json
    from store import get_triage_summary
    cached = get_triage_summary(job_id)
    if not cached:
        task = _summary_tasks.get(job_id, {})
        return JSONResponse({"status": "processing" if task.get("running") else "none"})
    summary = None
    if cached.get("summary_json"):
        try:
            summary = _json.loads(cached["summary_json"])
        except Exception:
            pass
    return JSONResponse({"status": cached.get("status", "done"), "summary": summary})


@app.post("/api/review/{job_id}/approve")
async def review_approve(job_id: str, background_tasks: BackgroundTasks, body: dict = {}) -> JSONResponse:
    """Move a triage job into the pipeline (status → recommended)."""
    from store import update_pipeline_entry, save_feedback
    update_pipeline_entry(job_id, status="recommended", verdict="recommend")

    # Positive feedback signal
    resume = get_latest_resume()
    if resume:
        save_feedback(job_id, resume["id"], "up", "triage_approved")

    return JSONResponse({"ok": True})


@app.post("/api/review/{job_id}/pass")
async def review_pass(job_id: str, background_tasks: BackgroundTasks, body: dict = {}) -> JSONResponse:
    """Mark a triage job as pass and store negative feedback."""
    from store import update_pipeline_entry, save_feedback
    reason = body.get("reason") or "triage_pass"

    update_pipeline_entry(job_id, status="pass", verdict="pass")

    resume = get_latest_resume()
    if resume:
        save_feedback(job_id, resume["id"], "down", reason)
        from resume_matcher import rescore_with_feedback
        background_tasks.add_task(rescore_with_feedback, resume["id"], job_id, "down", reason)

    return JSONResponse({"ok": True})


# ── Static / SPA ───────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/")
def root():
    return RedirectResponse(url="/review")


@app.get("/jobs")
def jobs_page() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/pipeline")
def pipeline_page() -> FileResponse:
    return FileResponse(str(_STATIC / "pipeline.html"))


@app.get("/coach")
def coach_page() -> FileResponse:
    return FileResponse(str(_STATIC / "coach.html"))


@app.get("/review")
def review_page() -> FileResponse:
    return FileResponse(str(_STATIC / "review.html"))


if __name__ == "__main__":
    # Parse --port from argv
    port = 8000
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv):
            port = int(sys.argv[i + 1])

    init_db()
    recover_stale_resumes()
    uvicorn.run(app, host="127.0.0.1", port=port)
