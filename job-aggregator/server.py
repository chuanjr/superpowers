#!/usr/bin/env python3
"""Local web server for the job board UI.

Usage:
  python server.py            # starts at http://localhost:8000
  python server.py --port 9000
"""
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from store import (
    init_db, get_all_jobs, save_resume, get_resume, get_latest_resume, get_matches,
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
        before = len(get_all_jobs())
        try:
            subprocess.run(
                [sys.executable, str(_ROOT / "main.py")],
                cwd=str(_ROOT),
                timeout=300,
            )
            after = len(get_all_jobs())
            _refresh_state["new_count"] = after - before
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


# ── Static / SPA ───────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/")
def root():
    return RedirectResponse(url="/pipeline")


@app.get("/jobs")
def jobs_page() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/pipeline")
def pipeline_page() -> FileResponse:
    return FileResponse(str(_STATIC / "pipeline.html"))


if __name__ == "__main__":
    # Parse --port from argv
    port = 8000
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv):
            port = int(sys.argv[i + 1])

    init_db()
    uvicorn.run(app, host="127.0.0.1", port=port)
