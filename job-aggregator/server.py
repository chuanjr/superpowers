#!/usr/bin/env python3
"""Local web server for the job board UI.

Usage:
  python server.py            # starts at http://localhost:8000
  python server.py --port 9000
"""
import sys
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from store import init_db, get_all_jobs, save_resume, get_resume, get_latest_resume, get_matches

app = FastAPI(title="Job Board")

_STATIC = Path(__file__).parent / "static"


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


# ── Static / SPA ───────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/")
def root() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


if __name__ == "__main__":
    # Parse --port from argv
    port = 8000
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv):
            port = int(sys.argv[i + 1])

    init_db()
    uvicorn.run(app, host="127.0.0.1", port=port)
