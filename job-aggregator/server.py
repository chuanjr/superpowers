#!/usr/bin/env python3
"""Local web server for the job board UI.

Usage:
  python server.py            # starts at http://localhost:8000
  python server.py --port 9000
"""
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from store import init_db, get_all_jobs

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


# Serve static files (CSS/JS assets if added later)
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
