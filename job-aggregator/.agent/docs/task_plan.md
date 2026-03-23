# Technical Roadmap

Last updated: 2026-03-23

## Immediate (Next Session)

### ATS Optimization Loop
- `POST /api/pipeline/{job_id}/optimize-resume` endpoint in `server.py`
- Re-runs `generate_ats_resume_sync()` with earned-secret coaching rules
- Re-runs `check_ats_sync()` for updated score
- Frontend (`pipeline.html`): if `ats_gap.score < 80` show "✨ Optimize Resume" button above textarea

### Resume Edit → Recheck ATS
- `POST /api/pipeline/{job_id}/recheck-ats` in `server.py` — accepts `{ats_resume: str}`
- Runs `check_ats_sync(ats_resume, job.description)`, upserts package, returns `ats_gap`
- Frontend: Save button triggers recheck, score badge updates inline

### Cover Letter from ATS Resume
- `POST /api/pipeline/{job_id}/regen-cover-letter` in `server.py`
- Reads `pkg.ats_resume` (falls back to `resume.raw_text`)
- Frontend: "↺ Regenerate" button in cover letter section

## Medium Term

### PDF Layout (1-page Compact)
- `server.py` — `download_resume_pdf()` function
- Letter page, 0.5" margins (36pt), 11pt body/13pt leading
- Shrink to 10pt if > 1 page after build
- `HRFlowable` rule after name/contact header

### Offer/Rejected Status
- Pipeline (`pipeline.html`): after `interviewing`, show "🎉 Offer" / "❌ Rejected" buttons
- `PATCH /api/pipeline/{job_id}` already accepts any status string

### Algorithm Tuning
- Use passed-job signals to tune match scoring weights
- Track why jobs were passed (keyword/company patterns in feedback table)

## Architecture Notes

- AI calls: `asyncio.to_thread()` wrapping sync Anthropic SDK calls
- Model: `claude-sonnet-4-6` — in `application_generator.py` lines ~518 and ~1086
- Gemini: `text-embedding-004` via `google-genai` SDK for resume/job embeddings
- Database: single `jobs.db` SQLite (~23MB live) — never commit
- Frontend: vanilla JS, no framework, `fetch()` API throughout
- Server start: `cd job-aggregator && venv/bin/uvicorn server:app --reload`
