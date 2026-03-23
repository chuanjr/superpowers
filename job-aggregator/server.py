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
from fastapi import Response

from dotenv import load_dotenv
load_dotenv(override=True)

from fastapi import BackgroundTasks, Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

from store import (
    init_db, recover_stale_resumes, get_all_jobs, save_resume, get_resume,
    get_latest_resume, get_latest_done_resume, get_matches,
    add_to_pipeline, get_pipeline, update_pipeline_entry, remove_from_pipeline,
    get_pipeline_job_ids, get_latest_resume_identity,
    get_job, save_feedback, update_resume,
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
    update_resume(resume_id, status="processing")
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

    # When called without a specific resume_id and the latest resume failed,
    # fall back to the most recent successful one so matches remain visible.
    if resume_id is None and resume and resume.get("status", "").startswith("error"):
        done = get_latest_done_resume()
        if done:
            resume = done

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

_refresh_state: dict = {"running": False, "new_count": None, "error": None, "cakeresume_auth_needed": False, "gmail_auth_needed": False}
_cake_auth_state: dict = {"running": False, "done": False, "error": None}


@app.post("/api/jobs/refresh")
def jobs_refresh() -> JSONResponse:
    if _refresh_state["running"]:
        return JSONResponse({"status": "already_running"})

    def _run() -> None:
        _refresh_state["running"] = True
        _refresh_state["error"] = None
        _refresh_state["new_count"] = None
        _refresh_state["gmail_auth_needed"] = False
        from store import get_existing_ids as _get_ids
        before_ids = _get_ids()
        try:
            result = subprocess.run(
                [sys.executable, str(_ROOT / "main.py")],
                cwd=str(_ROOT),
                timeout=300,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            output = result.stdout or ""
            if "[WARN] CakeResume" in output and "login may have expired" in output:
                _refresh_state["cakeresume_auth_needed"] = True
            if "[WARN] Gmail auth failed" in output:
                _refresh_state["gmail_auth_needed"] = True
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
        "cakeresume_auth_needed": _refresh_state.get("cakeresume_auth_needed", False),
        "gmail_auth_needed": _refresh_state.get("gmail_auth_needed", False),
    })


@app.get("/api/gmail/status")
def gmail_status() -> JSONResponse:
    """Check whether Gmail credentials are valid without triggering a refresh."""
    from pathlib import Path
    token_path = Path("credentials/token.json")
    if not token_path.exists():
        return JSONResponse({"connected": False, "reason": "no_token"})
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(token_path))
        if creds.valid:
            _refresh_state["gmail_auth_needed"] = False
            return JSONResponse({"connected": True})
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            _refresh_state["gmail_auth_needed"] = False
            return JSONResponse({"connected": True})
        return JSONResponse({"connected": False, "reason": "expired"})
    except Exception as exc:
        return JSONResponse({"connected": False, "reason": str(exc)})


@app.post("/api/gmail/reauth")
async def gmail_reauth() -> JSONResponse:
    """Run Gmail OAuth flow — opens a browser window on the local machine."""
    import asyncio, subprocess, sys
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: subprocess.run(
                    [sys.executable, "setup_gmail_auth.py"],
                    capture_output=True, text=True,
                )
            ),
            timeout=300.0,  # 5 min for user to complete OAuth
        )
        if result.returncode == 0:
            _refresh_state["gmail_auth_needed"] = False
            return JSONResponse({"ok": True})
        else:
            return JSONResponse({"ok": False, "error": result.stderr or result.stdout})
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "error": "Timed out — did you complete the sign-in?"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/auth/cakeresume")
def start_cakeresume_auth() -> JSONResponse:
    """Trigger the CakeResume re-authentication flow (opens browser window)."""
    if _cake_auth_state["running"]:
        return JSONResponse({"status": "already_running"})
    _cake_auth_state["running"] = True
    _cake_auth_state["done"] = False
    _cake_auth_state["error"] = None
    _refresh_state["cakeresume_auth_needed"] = False

    def _run():
        try:
            subprocess.run(
                [sys.executable, str(_ROOT / "setup_cakeresume_auth.py"), "--auto"],
                cwd=str(_ROOT),
                timeout=360,
            )
            _cake_auth_state["done"] = True
        except Exception as exc:
            _cake_auth_state["error"] = str(exc)
        finally:
            _cake_auth_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"status": "started"})


@app.get("/api/auth/cakeresume/status")
def cakeresume_auth_status() -> JSONResponse:
    return JSONResponse({
        "running": _cake_auth_state["running"],
        "done": _cake_auth_state["done"],
        "error": _cake_auth_state["error"],
    })


# ── URL import endpoints ───────────────────────────────────────────────────────

def _detect_market_from_url_and_company(url: str, company: str, location: str) -> str | None:
    """Heuristic market detection before sending to LLM — avoids TW false positives.

    Priority order:
    1. Known US companies → us (unless location explicitly says otherwise)
    2. URL domain (linkedin.com/in → check location; 104.com.tw → tw; cakeresume.com → check)
    3. Location keywords (Bay Area, San Francisco, New York, etc. → us; 台北 → tw; Tokyo → jp)
    """
    url_lower = (url or "").lower()
    company_lower = (company or "").lower()
    loc_lower = (location or "").lower()

    # Explicit TW/JP location keywords override company heuristic
    if any(k in loc_lower for k in ("台北", "taipei", "新北", "高雄", "台灣", "taiwan")):
        return "tw"
    if any(k in loc_lower for k in ("tokyo", "osaka", "東京", "大阪", "japan", "日本")):
        return "jp"
    if any(k in loc_lower for k in ("singapore", "sg", "싱가포르")):
        return "sg"

    # US location keywords
    if any(k in loc_lower for k in (
        "san francisco", "bay area", "menlo park", "seattle", "new york", "nyc",
        "los angeles", "austin", "boston", "chicago", "remote", "united states",
        "california", "washington", "new york city", "mountain view", "sunnyvale",
        "palo alto", "san jose",
    )):
        return "us"

    # Well-known US tech companies → default to US unless location says otherwise
    _US_COMPANIES = {
        "meta", "facebook", "google", "alphabet", "amazon", "apple", "microsoft",
        "netflix", "uber", "airbnb", "stripe", "openai", "anthropic", "linkedin",
        "twitter", "x corp", "snap", "snapchat", "pinterest", "reddit", "discord",
        "github", "figma", "notion", "salesforce", "oracle", "adobe", "nvidia",
        "intel", "amd", "qualcomm", "palantir", "datadog", "snowflake", "databricks",
        "confluent", "twilio", "okta", "workday", "zoom", "slack", "dropbox",
        "shopify", "robinhood", "coinbase", "doordash", "instacart", "lyft",
    }
    # Match company name loosely (strip Inc, Corp, etc.)
    company_clean = re.sub(r"\b(inc|corp|llc|ltd|co)\b\.?", "", company_lower).strip()
    for us_co in _US_COMPANIES:
        if us_co in company_clean or company_clean in us_co:
            return "us"

    # Taiwan job boards
    if "104.com.tw" in url_lower or "1111.com.tw" in url_lower:
        return "tw"
    if "cakeresume.com" in url_lower:
        return None  # CakeResume has both TW and global postings — let LLM decide

    return None  # Let LLM decide


def _extract_structured_data_from_html(html: str) -> dict:
    """Extract job data from HTML structured sources before stripping all tags.

    Looks for:
    - <script type="application/ld+json"> (LinkedIn, Indeed, CakeResume use this)
    - <meta property="og:title"> / <meta property="og:description">
    - LinkedIn-specific meta tags
    """
    import json as _json
    result: dict = {}

    # ── application/ld+json ────────────────────────────────────────────────────
    ld_matches = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    for ld_raw in ld_matches:
        try:
            ld = _json.loads(ld_raw.strip())
            # Handle @graph arrays
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if not isinstance(item, dict):
                    continue
                jtype = item.get("@type", "")
                if jtype == "JobPosting" or "JobPosting" in str(jtype):
                    result["title"] = item.get("title", result.get("title", ""))
                    result["company"] = (
                        item.get("hiringOrganization", {}).get("name", "")
                        or result.get("company", "")
                    )
                    loc = item.get("jobLocation", {})
                    if isinstance(loc, list):
                        loc = loc[0] if loc else {}
                    addr = loc.get("address", {}) if isinstance(loc, dict) else {}
                    if isinstance(addr, str):
                        result["location"] = addr
                    elif isinstance(addr, dict):
                        result["location"] = (
                            addr.get("addressLocality", "")
                            or addr.get("addressRegion", "")
                            or addr.get("addressCountry", "")
                        )
                    desc = item.get("description", "")
                    if desc:
                        # Strip HTML from description
                        desc = re.sub(r"<[^>]+>", " ", desc)
                        desc = re.sub(r"\s+", " ", desc).strip()
                        result["description"] = desc[:3000]
        except Exception:
            continue

    # ── og: meta tags ─────────────────────────────────────────────────────────
    if not result.get("title"):
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                       html, re.IGNORECASE)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                           html, re.IGNORECASE)
        if m:
            result["title"] = m.group(1).strip()

    if not result.get("description"):
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
                       html, re.IGNORECASE)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
                           html, re.IGNORECASE)
        if m:
            result["description"] = m.group(1).strip()

    # ── LinkedIn-specific: job-details div in SSR HTML ────────────────────────
    if "linkedin.com" in "":  # placeholder — SSR data is in __initialData__ script
        pass

    return result


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
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=headers)
        html = resp.text
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch URL: {exc}")

    # ── Extract structured data first (ld+json, og: meta) ─────────────────────
    structured = _extract_structured_data_from_html(html)

    # ── Strip HTML tags to plain text for LLM fallback ────────────────────────
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    # For LinkedIn: the text is mostly login-wall boilerplate.
    # Prepend any structured data we extracted so the LLM has something useful.
    is_linkedin = "linkedin.com" in url.lower()
    if is_linkedin and structured:
        structured_hint = "\n".join(
            f"{k}: {v}" for k, v in structured.items() if v
        )
        text = f"[STRUCTURED DATA EXTRACTED]\n{structured_hint}\n\n[RAW TEXT SNIPPET]\n{text[:2000]}"
    else:
        text = text[:6000]

    # ── Heuristic market pre-detection from URL domain ─────────────────────────
    url_market_hint = ""
    if "linkedin.com" in url.lower():
        url_market_hint = "\nNote: This is a LinkedIn URL. Location is usually a city in the posting, not Taiwan unless explicitly stated."
    elif "104.com.tw" in url.lower():
        url_market_hint = "\nNote: This is a 104.com.tw URL (Taiwan job board). Market is likely tw."

    # Claude parses the page
    ai = _anthropic.Anthropic()
    msg = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=900,
        messages=[{
            "role": "user",
            "content": f"""Extract job posting information from this page. Return ONLY valid JSON with these fields:
{{
  "title": "<job title>",
  "company": "<company name>",
  "location": "<city/region shown in the posting, e.g. 'San Francisco, CA' or 'Remote' or 'Taipei, Taiwan'>",
  "market": "<MUST be exactly one of: tw | jp | sg | us | null>",
  "description": "<full job description in original language, max 1500 chars>"
}}

MARKET DETECTION RULES (apply in order):
1. If location contains Taiwan / 台灣 / 台北 / Taipei → "tw"
2. If location contains Japan / 日本 / Tokyo / Osaka → "jp"
3. If location contains Singapore → "sg"
4. If company is Meta, Google, Amazon, Apple, Microsoft, Netflix, Uber, Airbnb, Stripe, OpenAI, Anthropic, LinkedIn, Snap, Pinterest, Reddit, Discord, Github, Figma, Notion, Salesforce, Nvidia → "us" (unless location explicitly says TW/JP/SG)
5. If location contains San Francisco, Bay Area, Seattle, New York, Los Angeles, Chicago, Boston, Austin, California, United States, Remote (US) → "us"
6. If genuinely unknown → null{url_market_hint}

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

    # ── Post-processing: override with structured data if LLM missed it ────────
    if structured.get("title") and not parsed.get("title"):
        parsed["title"] = structured["title"]
    if structured.get("company") and not parsed.get("company"):
        parsed["company"] = structured["company"]
    if structured.get("location") and not parsed.get("location"):
        parsed["location"] = structured["location"]
    # For description: prefer structured (cleaner) over LLM-extracted
    if structured.get("description") and len(structured["description"]) > len(parsed.get("description") or ""):
        parsed["description"] = structured["description"]

    # ── Final market heuristic override (prevent TW false positives) ───────────
    heuristic_market = _detect_market_from_url_and_company(
        url, parsed.get("company", ""), parsed.get("location", "")
    )
    if heuristic_market and not parsed.get("market"):
        parsed["market"] = heuristic_market
    elif heuristic_market == "us" and parsed.get("market") == "tw":
        # Heuristic strongly says US (known US company + US location) — override
        parsed["market"] = "us"

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


@app.patch("/api/candidate/culture/{culture_id}")
async def update_culture_entry(culture_id: int, body: dict, background_tasks: BackgroundTasks) -> JSONResponse:
    """Edit a culture entry's raw text and re-parse it."""
    raw_text = (body.get("raw_text") or "").strip()
    if not raw_text:
        raise HTTPException(400, "raw_text required")
    from store import update_culture_entry as _update
    _update(culture_id, raw_text)

    async def _reparse():
        from application_generator import parse_culture_sync
        from store import update_culture_parsed as _upd
        try:
            parsed = parse_culture_sync(raw_text)
            import json as _json
            _upd(culture_id, _json.dumps(parsed))
        except Exception:
            pass

    background_tasks.add_task(_reparse)
    return JSONResponse({"ok": True})


@app.delete("/api/candidate/culture/{culture_id}")
def delete_culture_entry(culture_id: int) -> JSONResponse:
    from store import delete_culture_entry as _del
    _del(culture_id)
    return JSONResponse({"ok": True})


@app.post("/api/candidate/culture/reorder")
def reorder_culture_entries(body: dict) -> JSONResponse:
    ordered_ids = body.get("ordered_ids") or []
    if not ordered_ids:
        raise HTTPException(400, "ordered_ids required")
    from store import reorder_culture_entries as _reorder
    _reorder(ordered_ids)
    return JSONResponse({"ok": True})


@app.post("/api/pipeline/{job_id}/culture-check")
async def pipeline_culture_check(job_id: str, background_tasks: BackgroundTasks) -> JSONResponse:
    """Run culture scoring for a pipeline job without generating the full package."""
    import json as _json
    from store import get_job, get_latest_resume, get_all_culture, upsert_application_package

    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    resume = get_latest_resume()
    if not resume:
        raise HTTPException(404, "No resume found")
    if not get_all_culture():
        raise HTTPException(400, "No culture entries found — add culture DNA first")

    async def _run():
        from application_generator import score_culture_sync
        fresh_culture_rows = get_all_culture()
        merged_dna = _build_culture_dna_from_rows(fresh_culture_rows)
        if not merged_dna:
            return

        jd_text = await _ensure_job_description(job_id, job)

        company_culture = None
        try:
            from company_research import get_or_research_company
            company_culture = await asyncio.wait_for(
                asyncio.to_thread(
                    get_or_research_company, job.get("company") or "", job.get("url") or ""
                ),
                timeout=60,
            )
        except Exception:
            pass  # company research is optional — proceed without it

        try:
            signals = await asyncio.wait_for(
                asyncio.to_thread(
                    score_culture_sync, merged_dna,
                    job.get("title") or "", job.get("company") or "",
                    jd_text, company_culture,
                ),
                timeout=60,
            )
            upsert_application_package(
                job_id, resume["id"],
                culture_score=signals.get("score"),
                culture_signals=_json.dumps(signals),
                status="done",
            )
        except Exception as exc:
            import traceback; traceback.print_exc()
            upsert_application_package(
                job_id, resume["id"],
                status="error",
            )

    background_tasks.add_task(_run)
    return JSONResponse({"ok": True, "status": "started"})


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
_co_tasks: dict  = {}  # company_key -> {"running": bool, "error": str|None}


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

    # Package exists but may be stuck in processing if task errored out or server restarted
    if pkg.get("status") in ("processing", "pending"):
        task_state = _pkg_tasks.get(job_id, {})
        if task_state.get("error"):
            return JSONResponse({"status": "error", "error": task_state["error"]})
        # No active task in memory (e.g. server restarted mid-generation) → allow retry
        if not task_state.get("running"):
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
        "ats_resume":               pkg.get("ats_resume"),
        "why_company":              pkg.get("why_company"),
        "value_prop":               pkg.get("value_prop"),
        "created_at":               pkg.get("created_at"),
        "custom_resume_filename":   pkg.get("custom_resume_filename"),
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
            # Auto-fetch JD from URL if description is missing
            refreshed_jd = await _ensure_job_description(job_id, job)
            refreshed_job = {**job, "description": refreshed_jd} if refreshed_jd else job
            await asyncio.wait_for(
                generate_package(job_id, resume["id"], refreshed_job, resume),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            _pkg_tasks[job_id]["error"] = "Generation timed out after 2 minutes. Please try again."
        except Exception as exc:
            import traceback; traceback.print_exc()
            _pkg_tasks[job_id]["error"] = str(exc)
        finally:
            _pkg_tasks[job_id]["running"] = False

    background_tasks.add_task(_run)
    return JSONResponse({"status": "started"})


@app.post("/api/pipeline/{job_id}/optimize-resume")
async def optimize_resume_endpoint(job_id: str, request: Request) -> JSONResponse:
    """Deep-optimize ATS resume — dual-lens (ATS + senior recruiter), score regression guard."""
    import json as _json
    try:
        body = await request.json()
    except Exception:
        body = {}
    coach_answers: list[dict] = body.get("coach_answers", [])  # [{question, answer}, ...]
    resume = get_latest_resume()
    if not resume:
        raise HTTPException(404, "No resume found")
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    from store import get_application_package, upsert_application_package
    from application_generator import optimize_resume_deep_sync, check_ats_sync, compute_resume_diff

    pkg = get_application_package(job_id, resume["id"])
    if not pkg:
        raise HTTPException(400, "Generate the full package first")

    # Use per-job custom resume if uploaded, else existing ats_resume, else global resume
    base_text = pkg.get("custom_resume_text") or pkg.get("ats_resume") or resume.get("raw_text", "")
    ats_gap_raw = pkg.get("ats_gap") or {}
    if isinstance(ats_gap_raw, str):
        try:
            ats_gap_raw = _json.loads(ats_gap_raw)
        except Exception:
            ats_gap_raw = {}

    original_score: int = ats_gap_raw.get("score") or 0

    jd_text = job.get("description") or ""
    if not jd_text:
        jd_text = await _ensure_job_description(job_id, job)

    # Pass story bank so the optimizer can reference real achievements
    from store import get_stories
    stories = get_stories()
    story_context = "\n".join(
        f"- {s.get('situation','')}: {s.get('action','')}: {s.get('result','')}"
        for s in (stories or []) if s.get("action")
    )[:2000]

    # Append coach Q&A answers as clarifying story context
    if coach_answers:
        qa_block = "\n\n═══ COACH Q&A — use these answers to write more specific bullets ═══\n"
        qa_block += "\n".join(
            f"Q: {qa.get('question','')}\nA: {qa.get('answer','')}"
            for qa in coach_answers if qa.get("answer")
        )
        story_context = (story_context + qa_block)[:4000]

    import asyncio
    loop = asyncio.get_event_loop()

    # ── Score regression guard: try up to 2 times, keep best ────────────────────
    # Allow minor drops (≤5 pts) when authentic earned-secret bullets are better quality.
    # A 5-point drop from keyword cleanup is acceptable; a large drop means something broke.
    _ACCEPTABLE_DROP = 5
    best_resume = base_text
    best_gap = ats_gap_raw
    best_score = original_score
    best_opt_result: dict = {}

    for attempt in range(2):
        opt_result = await loop.run_in_executor(
            None, lambda: optimize_resume_deep_sync(base_text, jd_text, ats_gap_raw,
                                                    story_context=story_context)
        )
        candidate_resume = opt_result["resume"]
        candidate_gap = await loop.run_in_executor(
            None, lambda: check_ats_sync(candidate_resume, jd_text)
        )
        candidate_score: int = candidate_gap.get("score") or 0

        # Accept if score improved OR only dropped within acceptable margin
        score_ok = candidate_score >= (original_score - _ACCEPTABLE_DROP)
        if score_ok and candidate_score >= (best_score or 0) - _ACCEPTABLE_DROP:
            best_resume = candidate_resume
            best_gap = candidate_gap
            best_score = candidate_score
            best_opt_result = opt_result
            if score_ok:
                break  # Acceptable result — stop here
        elif attempt == 1:
            # Both attempts regressed too much — fall back to original
            best_opt_result = {"coach_questions": opt_result.get("coach_questions", []),
                               "location_note": opt_result.get("location_note")}

    # ── Compute bullet-level diff ─────────────────────────────────────────────────
    diff = compute_resume_diff(base_text, best_resume)

    # ── Save best version ─────────────────────────────────────────────────────────
    upsert_application_package(job_id, resume["id"],
                               ats_resume=best_resume,
                               ats_gap=_json.dumps(best_gap))

    return JSONResponse({
        "ats_resume": best_resume,
        "ats_gap": best_gap,
        "diff": diff,
        "score_improved": best_score >= original_score,
        "original_score": original_score,
        "coach_questions": best_opt_result.get("coach_questions", []),
        "coach_conflicts": best_opt_result.get("coach_conflicts", []),
        "location_note": best_opt_result.get("location_note"),
        "market": best_opt_result.get("market", "UNKNOWN"),
    })


@app.post("/api/pipeline/{job_id}/upload-resume")
async def upload_custom_resume(job_id: str, file: UploadFile = File(...)) -> JSONResponse:
    """Upload a position-specific resume PDF for this job's ATS check."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")
    from resume_matcher import extract_pdf_text
    from store import get_application_package, upsert_application_package
    resume = get_latest_resume()
    if not resume:
        raise HTTPException(404, "No global resume found — upload a resume first")
    data = await file.read()
    raw_text = extract_pdf_text(data)
    if not raw_text or len(raw_text.strip()) < 50:
        raise HTTPException(422, "Could not extract text from PDF")
    pkg = get_application_package(job_id, resume["id"])
    if not pkg:
        raise HTTPException(400, "Generate the application package first")
    upsert_application_package(
        job_id, resume["id"],
        custom_resume_text=raw_text,
        custom_resume_filename=file.filename,
        # Seed ats_resume with the custom text so the textarea shows it immediately
        ats_resume=raw_text,
    )
    return JSONResponse({"ok": True, "filename": file.filename, "length": len(raw_text)})


@app.post("/api/pipeline/{job_id}/recheck-ats")
async def recheck_ats_endpoint(job_id: str, body: dict) -> JSONResponse:
    """Re-run ATS keyword check on edited resume text and save the new score."""
    import json as _json
    resume = get_latest_resume()
    if not resume:
        raise HTTPException(404, "No resume found")
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    from store import upsert_application_package
    from application_generator import check_ats_sync

    ats_resume = (body.get("ats_resume") or "").strip()
    if not ats_resume:
        raise HTTPException(400, "ats_resume is required")

    jd_text = job.get("description") or ""
    if not jd_text:
        jd_text = await _ensure_job_description(job_id, job)

    import asyncio
    loop = asyncio.get_event_loop()
    new_gap = await loop.run_in_executor(None, lambda: check_ats_sync(ats_resume, jd_text))
    upsert_application_package(job_id, resume["id"],
                               ats_resume=ats_resume,
                               ats_gap=_json.dumps(new_gap))
    return JSONResponse({"ats_gap": new_gap})


@app.post("/api/pipeline/{job_id}/regen-cover-letter")
async def regen_cover_letter_endpoint(job_id: str, body: dict = Body(default={})) -> JSONResponse:
    """Regenerate value proposition from latest ATS resume content."""
    import json as _json
    resume = get_latest_resume()
    if not resume:
        raise HTTPException(404, "No resume found")
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    from store import get_application_package, upsert_application_package, get_all_culture
    from application_generator import write_value_prop_sync

    pkg = get_application_package(job_id, resume["id"])
    if not pkg:
        raise HTTPException(400, "Generate the full package first")

    base_text = pkg.get("ats_resume") or resume.get("raw_text", "")
    jd_text = job.get("description") or ""
    if not jd_text:
        jd_text = await _ensure_job_description(job_id, job)

    # Get story matches from existing package for context
    story_matches_raw = pkg.get("story_matches") or "[]"
    if isinstance(story_matches_raw, str):
        try:
            story_matches = _json.loads(story_matches_raw)
        except Exception:
            story_matches = []
    else:
        story_matches = story_matches_raw or []

    parsed = _json.loads(resume.get("parsed_json") or "{}")
    resume_summary = parsed.get("summary") or ""

    user_why = (body.get("why_input") or "").strip()

    import asyncio
    loop = asyncio.get_event_loop()
    new_value_prop = await loop.run_in_executor(
        None,
        lambda: write_value_prop_sync(
            resume_summary, story_matches,
            job.get("title", ""), job.get("company", ""), jd_text,
            user_why=user_why,
        )
    )
    upsert_application_package(job_id, resume["id"], value_prop=new_value_prop)
    return JSONResponse({"value_prop": new_value_prop})


@app.post("/api/pipeline/{job_id}/resume-pdf")
async def download_resume_pdf(job_id: str, body: dict) -> Response:
    """Convert ATS-optimized resume text to a styled PDF and return for download."""
    import io, re as _re
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    import html as _html_mod
    from reportlab.platypus import HRFlowable, KeepTogether
    from reportlab.lib.pagesizes import letter

    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content is required")

    # Known resume section keywords — treated as section headers in PDF
    _SECTION_KEYWORDS = {
        "summary", "professional summary", "profile", "objective",
        "experience", "work experience", "professional experience", "employment",
        "education", "academic background",
        "skills", "technical skills", "core competencies", "key skills",
        "projects", "certifications", "awards", "languages",
        "publications", "volunteer", "activities",
    }

    def _is_pdf_section(line: str) -> bool:
        """True if this line should be rendered as a section header in the PDF."""
        s = line.strip()
        if s.startswith("## ") or s.startswith("# "):
            return True
        lower = s.lower()
        if lower in _SECTION_KEYWORDS:
            return True
        # All-caps short line (e.g. "WORK EXPERIENCE", "SKILLS")
        if 2 <= len(s) <= 50 and re.match(r'^[A-Z][A-Z\s&/\-]+$', s):
            return True
        return False

    def _md_to_rl(text: str) -> str:
        """Convert basic markdown inline syntax to ReportLab XML markup.
        Must be called AFTER html.escape to avoid double-escaping.
        """
        # **bold** → <b>bold</b>
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        # *italic* → <i>italic</i>  (only remaining single stars)
        text = re.sub(r'\*([^\*]+?)\*', r'<i>\1</i>', text)
        return text

    def _build_story(body_pt: float, bullet_pt: float):
        """Build the reportlab story at the given font sizes."""
        styles = getSampleStyleSheet()
        name_style = ParagraphStyle(
            "Name", parent=styles["Normal"],
            fontName="Helvetica-Bold", fontSize=15,
            spaceAfter=1, leading=18,
        )
        contact_style = ParagraphStyle(
            "Contact", parent=styles["Normal"],
            fontName="Helvetica", fontSize=body_pt - 0.5,
            spaceAfter=1, leading=(body_pt - 0.5) * 1.2,
            textColor=colors.HexColor("#555555"),
        )
        section_style = ParagraphStyle(
            "Section", parent=styles["Normal"],
            fontName="Helvetica-Bold", fontSize=body_pt,
            spaceBefore=6, spaceAfter=2,
            textColor=colors.HexColor("#1a1a1a"),
        )
        role_style = ParagraphStyle(
            "Role", parent=styles["Normal"],
            fontName="Helvetica-Bold", fontSize=body_pt,
            spaceAfter=0, leading=body_pt * 1.2,
        )
        body_s = ParagraphStyle(
            "Body", parent=styles["Normal"],
            fontName="Helvetica", fontSize=body_pt,
            spaceAfter=1, leading=body_pt * 1.25,
        )
        bullet_s = ParagraphStyle(
            "Bullet", parent=styles["Normal"],
            fontName="Helvetica", fontSize=bullet_pt,
            spaceAfter=1, leading=bullet_pt * 1.25,
            leftIndent=12, bulletIndent=2,
        )

        story: list = []
        lines = content.split("\n")
        first_line = True
        in_header = True   # pre-EXPERIENCE contact zone
        section_block: list = []

        def flush_section():
            if section_block:
                story.append(KeepTogether(list(section_block)))
                section_block.clear()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                if section_block:
                    section_block.append(Spacer(1, 2))
                continue

            # ── First non-blank line = candidate name ──────────────────────────
            if first_line:
                # Strip any markdown bold from the name
                name_text = re.sub(r'\*\*(.+?)\*\*', r'\1', stripped)
                story.append(Paragraph(_html_mod.escape(name_text), name_style))
                story.append(HRFlowable(width="100%", thickness=0.5,
                                        color=colors.HexColor("#cccccc"), spaceAfter=3))
                first_line = False
                continue

            # ── Section header ─────────────────────────────────────────────────
            if _is_pdf_section(stripped):
                flush_section()
                in_header = False
                raw_title = stripped.lstrip("#").strip()
                # Strip markdown bold from section title
                raw_title = re.sub(r'\*\*(.+?)\*\*', r'\1', raw_title)
                section_block.append(Paragraph(_html_mod.escape(raw_title).upper(), section_style))
                section_block.append(HRFlowable(width="100%", thickness=0.3,
                                                 color=colors.HexColor("#e0e0e0"), spaceAfter=2))
                continue

            # ── Bullet line ────────────────────────────────────────────────────
            if stripped.startswith("- ") or stripped.startswith("• "):
                bullet_text = stripped[2:].strip()
                rl_text = _md_to_rl(_html_mod.escape(bullet_text))
                section_block.append(Paragraph(f"• {rl_text}", bullet_s))
                continue

            # ── Contact / header zone (before first section) ──────────────────
            if in_header:
                rl_text = _md_to_rl(_html_mod.escape(stripped))
                story.append(Paragraph(rl_text, contact_style))
                continue

            # ── Role / company line (bold-markdown or bare text in body) ──────
            if stripped.startswith("**") and stripped.endswith("**"):
                # **Role Title** — treat as a role header
                role_text = stripped.strip("*")
                section_block.append(Paragraph(_html_mod.escape(role_text), role_style))
            else:
                rl_text = _md_to_rl(_html_mod.escape(stripped))
                section_block.append(Paragraph(rl_text, body_s))

        flush_section()
        return story

    # Try progressively smaller font sizes until fits on 1 page
    pdf_bytes = b""
    for body_pt, bullet_pt in [(10.0, 9.5), (8.5, 8.0), (7.5, 7.0)]:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=letter,
            leftMargin=36, rightMargin=36,
            topMargin=28, bottomMargin=28,
        )
        story_elements = _build_story(body_pt, bullet_pt)
        try:
            doc.build(story_elements)
        except Exception as exc:
            raise HTTPException(500, f"PDF generation error: {exc}")
        pdf_bytes = buf.getvalue()
        # doc.page holds the last page number after build
        if getattr(doc, "page", 2) <= 1:
            break  # fits on 1 page

    safe_id = re.sub(r"[^a-z0-9]", "-", job_id.lower())[:40]
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="resume-{safe_id}.pdf"'},
    )


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

def _jd_looks_garbage(desc: str) -> bool:
    """Return True if a cached description looks like raw HTML/script content."""
    if not desc:
        return True
    # JSON-LD markers in the text = script tag content was not stripped
    if '"@context"' in desc or '"@type"' in desc:
        return True
    # Navigation/login page content (job board menu, not actual JD)
    nav_signals = ["登入", "Log in\n", "Sign in\n", "Register\n", "function getDfd()", "window.lazyloader"]
    if any(sig in desc[:300] for sig in nav_signals):
        return True
    # Very short content
    if len(desc.split()) < 20 and len(desc) < 150:
        return True
    return False


async def _ensure_job_description(job_id: str, job: dict) -> str:
    """Return job description, auto-fetching from URL if empty or garbage-looking."""
    desc = job.get("description") or ""
    url = job.get("url") or ""
    # Skip company overview pages — they never contain a job description
    # cake.me/companies/X = company page; yourator.co/companies/X/jobs/Y = job page
    import re as _re
    _is_company_page = (
        _re.search(r"cake\.me/companies/[^/]+$", url) or  # CakeResume company page (no /jobs/ suffix)
        "linkedin.com/company/" in url  # LinkedIn company page
    )
    url_is_job_page = url and not _is_company_page
    if (_jd_looks_garbage(desc)) and url_is_job_page:
        try:
            from company_research import fetch_jd_from_url
            from store import update_job_description as _update_jd
            fetched = await asyncio.to_thread(fetch_jd_from_url, job["url"])
            # Require at least 80 words of meaningful content — login-wall pages
            # (LinkedIn, etc.) return boilerplate HTML that is useless for ATS scoring
            # Accept if 80+ space-split tokens (English) OR 150+ chars (Chinese/CJK)
            if fetched and (len(fetched.split()) >= 80 or len(fetched) >= 150):
                _update_jd(job_id, fetched)
                return fetched
        except Exception:
            pass
    return desc


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


@app.get("/api/jobs/count")
def jobs_count(since: str = "") -> JSONResponse:
    from datetime import datetime, timedelta, timezone
    jobs = get_all_jobs()
    if since == "7d":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        jobs = [j for j in jobs if (j.get("first_seen_at") or "") >= cutoff]
    return JSONResponse({"count": len(jobs)})


# ── Config read/write endpoints ────────────────────────────────────────────────

_CONFIG_PATH = _ROOT / "config.yaml"

_ALL_MARKETS = ["tw", "us", "jp", "sg"]
_MARKET_LABELS = {"tw": "🇹🇼 Taiwan", "us": "🇺🇸 USA", "jp": "🇯🇵 Japan", "sg": "🇸🇬 Singapore"}
_ALL_SOURCES = ["104", "cakeresume", "yourator", "linkedin_jobs", "linkedin_gmail",
                "indeed_rss", "indeed_gmail", "wellfound"]
_SOURCE_LABELS = {
    "104": "104 (台灣)",
    "cakeresume": "CakeResume",
    "yourator": "Yourator",
    "linkedin_jobs": "LinkedIn Jobs",
    "linkedin_gmail": "LinkedIn (Email alerts)",
    "indeed_rss": "Indeed (RSS)",
    "indeed_gmail": "Indeed (Email alerts)",
    "wellfound": "Wellfound (AngelList)",
}


@app.get("/api/config")
def get_app_config() -> JSONResponse:
    """Return current markets, sources, and search titles from config.yaml."""
    import yaml as _yaml
    try:
        with open(_CONFIG_PATH) as f:
            cfg = _yaml.safe_load(f)
    except Exception as exc:
        raise HTTPException(500, f"Could not read config: {exc}")
    return JSONResponse({
        "markets": cfg.get("markets", []),
        "sources": cfg.get("sources", {}),
        "titles": cfg.get("targets", {}).get("titles", []),
        "exclude_keywords": cfg.get("targets", {}).get("exclude_keywords", []),
        "all_markets": _ALL_MARKETS,
        "market_labels": _MARKET_LABELS,
        "all_sources": _ALL_SOURCES,
        "source_labels": _SOURCE_LABELS,
    })


@app.patch("/api/config")
async def update_app_config(body: dict) -> JSONResponse:
    """Patch config.yaml — accepts markets (list), sources (dict), titles (list)."""
    import yaml as _yaml
    try:
        with open(_CONFIG_PATH) as f:
            cfg = _yaml.safe_load(f)
    except Exception as exc:
        raise HTTPException(500, f"Could not read config: {exc}")

    changed = False

    if "markets" in body:
        new_markets = [m for m in body["markets"] if m in _ALL_MARKETS]
        if new_markets != cfg.get("markets"):
            cfg["markets"] = new_markets
            changed = True

    if "sources" in body and isinstance(body["sources"], dict):
        if "sources" not in cfg:
            cfg["sources"] = {}
        for k, v in body["sources"].items():
            if k in _ALL_SOURCES and isinstance(v, bool):
                cfg["sources"][k] = v
                changed = True

    if "titles" in body and isinstance(body["titles"], list):
        titles = [t.strip() for t in body["titles"] if isinstance(t, str) and t.strip()]
        if titles:
            cfg.setdefault("targets", {})["titles"] = titles
            changed = True

    if "exclude_keywords" in body and isinstance(body["exclude_keywords"], list):
        kws = [k.strip() for k in body["exclude_keywords"] if isinstance(k, str) and k.strip()]
        cfg.setdefault("targets", {})["exclude_keywords"] = kws
        changed = True

    if changed:
        try:
            with open(_CONFIG_PATH, "w") as f:
                _yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        except Exception as exc:
            raise HTTPException(500, f"Could not write config: {exc}")

    return JSONResponse({"ok": True, "changed": changed, "config": {
        "markets": cfg.get("markets", []),
        "sources": cfg.get("sources", {}),
        "titles": cfg.get("targets", {}).get("titles", []),
    }})


@app.get("/api/review/count")
def review_count(since: str = "") -> JSONResponse:
    from store import get_triage
    from datetime import datetime, timedelta, timezone
    entries = get_triage()
    if since == "7d":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        entries = [e for e in entries if (e.get("added_at") or "") >= cutoff]
    return JSONResponse({"count": len(entries)})


@app.get("/api/review")
def review_list() -> JSONResponse:
    """Return all jobs in the triage queue, ordered by match score."""
    import json as _json
    from store import get_triage
    import json as _json
    entries = get_triage()
    for e in entries:
        e.pop("description", None)
        # Backfill culture_score from triage summary if package hasn't been generated yet
        if e.get("culture_score") is None and e.get("summary_json"):
            try:
                sj = _json.loads(e["summary_json"])
                cs = sj.get("culture_score")
                if cs is not None:
                    e["culture_score"] = cs
            except Exception:
                pass
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
        try:
            from application_generator import generate_triage_summary_sync
            from store import get_all_culture, upsert_triage_summary as _upsert
            parsed = _json.loads(resume.get("parsed_json") or "{}")
            resume_summary = parsed.get("summary") or ""

            # Get existing match explanation if available
            from store import get_matches
            matches = get_matches(resume["id"])
            explanation = next((m["explanation"] for m in matches if m["job_id"] == job_id), None)

            culture_rows = get_all_culture()
            culture_dna = _build_culture_dna_from_rows(culture_rows)

            # Auto-fetch JD if description is empty
            job_description = await _ensure_job_description(job_id, job)

            # Fetch company culture profile (cached)
            company_culture = None
            try:
                from company_research import get_or_research_company
                company_culture = await asyncio.to_thread(
                    get_or_research_company, job.get("company") or "",
                    job.get("url") or "",
                )
            except Exception:
                pass

            summary = await asyncio.to_thread(
                generate_triage_summary_sync,
                resume_summary, job_description,
                job.get("title") or "", job.get("company") or "",
                culture_dna, explanation, company_culture,
            )
            _upsert(job_id, resume["id"], _json.dumps(summary), "done")
        except Exception as exc:
            try:
                from store import upsert_triage_summary as _upsert
                _upsert(job_id, resume["id"], _json.dumps({"error": str(exc)}), "error")
            except Exception:
                pass
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
    """Move a triage job into the pipeline (status → recommended) and auto-generate package."""
    from store import update_pipeline_entry, save_feedback
    update_pipeline_entry(job_id, status="recommended", verdict="recommend")

    resume = get_latest_resume()
    if resume:
        # Positive feedback signal + rescore
        save_feedback(job_id, resume["id"], "up", "triage_approved")
        from resume_matcher import rescore_with_feedback
        background_tasks.add_task(rescore_with_feedback, resume["id"], job_id, "up", "triage_approved")

        # Auto-generate application package in background (skip if already generating)
        job = get_job(job_id)
        if job and not _pkg_tasks.get(job_id, {}).get("running"):
            _pkg_tasks[job_id] = {"running": True, "error": None}

            async def _auto_gen():
                import asyncio
                from application_generator import generate_package
                try:
                    refreshed_jd = await _ensure_job_description(job_id, job)
                    refreshed_job = {**job, "description": refreshed_jd} if refreshed_jd else job
                    await asyncio.wait_for(
                        generate_package(job_id, resume["id"], refreshed_job, resume),
                        timeout=120.0,
                    )
                except asyncio.TimeoutError:
                    _pkg_tasks[job_id]["error"] = "Auto-generation timed out."
                except Exception as exc:
                    import traceback; traceback.print_exc()
                    _pkg_tasks[job_id]["error"] = str(exc)
                finally:
                    _pkg_tasks[job_id]["running"] = False

            background_tasks.add_task(_auto_gen)

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


@app.get("/setup")
def setup_page() -> FileResponse:
    return FileResponse(str(_STATIC / "setup.html"))


@app.post("/api/review/{job_id}/jd")
async def review_set_jd(job_id: str, body: dict) -> JSONResponse:
    """Save a manually-pasted job description, then invalidate the cached brief."""
    from store import update_job_description, upsert_jd_brief
    desc = (body.get("description") or "").strip()
    if not desc:
        raise HTTPException(400, "description is required")
    update_job_description(job_id, desc)
    upsert_jd_brief(job_id, "")   # invalidate cached brief so it regenerates
    return JSONResponse({"ok": True})


@app.get("/api/review/{job_id}/jd")
async def review_get_jd(job_id: str) -> JSONResponse:
    """Return (and auto-fetch if missing) the JD for a triage job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    description = await _ensure_job_description(job_id, job)
    return JSONResponse({"description": description or ""})


@app.get("/api/review/{job_id}/jd-brief")
async def review_get_jd_brief(job_id: str) -> JSONResponse:
    """Return a plain-language AI summary of the JD. Generates and caches on first call."""
    from store import get_jd_brief, upsert_jd_brief
    cached = get_jd_brief(job_id)
    if cached:
        return JSONResponse({"brief": cached})

    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    description = await _ensure_job_description(job_id, job)

    def _generate_brief(desc: str, title: str, company: str) -> str:
        import anthropic as _anthropic
        client = _anthropic.Anthropic()
        has_real_jd = desc and len(desc.strip()) >= 100
        jd_section = f"Job Description:\n{desc[:3000]}" if has_real_jd else \
            "(Job description not available — infer from job title and company context only)"
        prompt = f"""You are a senior career coach giving a candidate a quick, honest read on a job.

Based on the information below, write 3-5 sentences explaining what this job actually involves day-to-day.
Cover: what the person works on, who they collaborate with, and what success looks like.
Be direct and specific — no fluff, no bullet headers, no asking for more info.
If the JD is missing, make educated inferences from the job title and company.
NEVER ask the user to provide more information. Just give your best assessment.
Write in English unless the job description is primarily in Chinese, then write in Chinese.

Job: {title} at {company}
{jd_section}"""
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    brief = await asyncio.to_thread(
        _generate_brief, description, job.get("title", ""), job.get("company", "")
    )
    upsert_jd_brief(job_id, brief)
    return JSONResponse({"brief": brief})


# ── Company culture research endpoints ──────────────────────────────────────────

@app.get("/api/company/{company_key}/culture")
def get_company_culture(company_key: str) -> JSONResponse:
    """Return cached company culture profile."""
    import json as _json
    from store import get_company_culture_cache
    cached = get_company_culture_cache(company_key)
    if not cached:
        task_state = _co_tasks.get(company_key, {})
        if task_state.get("running"):
            return JSONResponse({"status": "processing"})
        if task_state.get("error"):
            return JSONResponse({"status": "error", "error": task_state["error"]})
        return JSONResponse({"status": "none"})
    parsed = None
    if cached.get("parsed_json"):
        try:
            parsed = _json.loads(cached["parsed_json"])
        except Exception:
            pass
    return JSONResponse({
        "status": "done",
        "company": cached.get("company"),
        "fetched_at": cached.get("fetched_at"),
        "culture": parsed,
    })


@app.post("/api/company/{company_key}/research")
async def research_company_culture(company_key: str, background_tasks: BackgroundTasks,
                                    body: dict = {}) -> JSONResponse:
    """Trigger company culture research (web search + Claude parse). force=true to refresh."""
    import json as _json
    from store import get_company_culture_cache
    company = body.get("company") or company_key.replace("-", " ").title()
    job_url = body.get("job_url") or ""
    force = bool(body.get("force", False))

    if not force:
        cached = get_company_culture_cache(company_key)
        if cached and cached.get("parsed_json"):
            return JSONResponse({"status": "cached", "fetched_at": cached.get("fetched_at")})

    _co_tasks[company_key] = {"running": True, "error": None}

    async def _run():
        from company_research import get_or_research_company
        try:
            await asyncio.to_thread(get_or_research_company, company, job_url, force=True)
        except Exception as exc:
            _co_tasks[company_key]["error"] = str(exc)
        finally:
            _co_tasks[company_key]["running"] = False

    background_tasks.add_task(_run)
    return JSONResponse({"status": "started"})


if __name__ == "__main__":
    # Parse --port from argv
    port = 8000
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv):
            port = int(sys.argv[i + 1])

    init_db()
    recover_stale_resumes()
    uvicorn.run(app, host="127.0.0.1", port=port)
