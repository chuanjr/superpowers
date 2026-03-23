# Implementation Progress

Last updated: 2026-03-23

## ✅ Completed

### Core Infrastructure
- FastAPI server (`server.py`, ~1,966 lines) with 46+ API endpoints
- SQLite store (`store.py`) — jobs, resumes, matches, pipeline, culture, stories
- Job fetching pipeline: Gmail API (LinkedIn/Indeed), RSS feeds, Playwright scraper (104/CakeResume/Yourator/Wellfound)
- Deduplication via MD5(company::title)
- Daily aggregation CLI (`main.py`)
- Gemini embedding for semantic job-resume matching
- `config.yaml` with `candidate.name` field (personalizes cover letters)

### Frontend
- `/jobs` (index.html) — job board with filters; stat cards deeplink to pages
- `/review` (review.html) — triage queue, sorted by match score
- `/pipeline` (pipeline.html) — full pipeline tracker with funnel stats
- `/setup` (setup.html) — config wizard with resume upload + status polling
- `/coach` (coach.html) — resume coaching interface

### AI Features
- Cover letter generation via CoverLetter_Framework.md 5-paragraph structure (P1 why company, P2 core story, P3 logical connection, P4 why here/now)
- Dynamic "why I care" prompts in pipeline drawer based on role keywords (growth/consumer/0to1/B2B)
- ATS gap analysis + ATS resume optimizer
- Culture fit scoring (culture-check button in pipeline drawer)
- STAR story matching
- Resume section splitter + market-specific header formatting (TW/JP/SG/US)

### Auth & Integration
- Gmail OAuth with token refresh (`GET /api/gmail/status` reads token.json)
- Gmail auth banner: auto-hides after connect, X-button dismissal persists via `_gmailBannerDismissed`, page-load status check
- CakeResume Playwright auth
- `POST /api/gmail/reauth` spawns OAuth subprocess

### Bug Fixes (recent)
- Resume upload "failed" → fixed stale `renderPkgDrawer` call → `loadPackage` (commit 2a423e7)
- Gmail banner not hiding → `_gmailBannerDismissed` flag + `hideGmailBanner()`
- Model not found (`claude-sonnet-4-5-20251001`) → updated to `claude-sonnet-4-6` (commit e48adfd)
- Pipeline stat cards → `a.stat-card` with hover lift + blue border deeplinks
- Culture column removed from pipeline table (not actionable; still accessible in drawer)

## 📋 Backlog

- ATS < 80 → "✨ Optimize Resume" button (see task_plan.md)
- Resume edit re-checks ATS score inline
- Cover letter regenerates from latest ATS resume
- Offer / Rejected status after Interviewing
- PDF compact 1-page layout
