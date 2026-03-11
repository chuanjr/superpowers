# Job Aggregator Design Spec

**Date:** 2026-03-11
**Status:** Approved

## Overview

A local automation tool that aggregates job listings from multiple platforms across Taiwan, Japan, and Singapore, filters them by relevance using Claude Haiku, and delivers a daily deduplicated summary email.

## Problem Statement

Manually checking multiple job platforms and company websites is time-consuming and passive. Users are limited to opportunities from platforms they already know. This tool solves:

1. **Too many platforms to check manually** — aggregate 104, CakeResume, Yourator, Wellfound, Indeed (multi-region), and LinkedIn into one daily digest
2. **Limited company discovery** — surface relevant opportunities from platforms and regions the user might not actively monitor

## Target Markets

- Taiwan
- Japan
- Singapore

(US market excluded from initial scope; addable via config later)

## Data Sources

| Platform | Method | Markets |
|----------|--------|---------|
| LinkedIn | Gmail alert (OAuth) | TW, JP, SG |
| Indeed | Gmail alert + RSS (tw/jp/sg.indeed.com) | TW, JP, SG |
| 104 | RSS feed | TW |
| Wellfound | RSS feed | TW, SG (locations: NY, SF, LA, Remote) |
| CakeResume | Web scraper | TW |
| Yourator | Web scraper | TW |

## Architecture

```
job-aggregator/
├── main.py                  # Entry point, orchestrates daily run
├── setup.py                 # Interactive CLI for managing config
├── fetchers/
│   ├── gmail_fetcher.py     # Gmail API: fetch LinkedIn + Indeed alerts
│   ├── rss_fetcher.py       # RSS: 104, Wellfound, Indeed feeds
│   └── scraper.py           # Playwright: CakeResume, Yourator
├── parser.py                # Normalize all sources to unified Job schema
├── matcher.py               # Claude Haiku: filter by industry + job title
├── enricher.py              # Company info: Wellfound native + Haiku inference
├── deduplicator.py          # Cross-platform dedup by company + title
├── notifier.py              # Compose + send summary email via Gmail API
├── state.json               # Processed job IDs, prevents reappearance
├── config.yaml              # User settings (editable anytime)
└── credentials/
    ├── client_secret.json   # Google OAuth credentials
    └── token.json           # Auto-generated after first auth
```

## Data Flow

```
Fetchers (Gmail / RSS / Scraper)
    ↓
Parser → unified Job schema
    ↓
Deduplicator → remove cross-platform + previously seen duplicates
    ↓
Matcher (Claude Haiku) → filter: ① software industry ② title/experience match
    ↓
Enricher → add company industry + stage
    ↓
Notifier → send daily summary email
    ↓
state.json updated
```

## Unified Job Schema

```python
{
  "id": str,               # hash of company + title
  "title": str,
  "company": str,
  "location": str,
  "market": str,           # "tw" | "jp" | "sg"
  "url": str,
  "description": str,
  "source": list[str],     # ["linkedin", "104"] if deduped across platforms
  "industry": str,         # e.g. "marketplace", "crypto", "social"
  "stage": str,            # e.g. "seed", "series_b", "public"
  "fetched_at": str        # ISO datetime
}
```

## Filtering (Claude Haiku)

Two-layer filter per job listing, single API call per job:

1. **Industry filter** — is this a software company? (exclude finance, manufacturing, etc.)
2. **Role match** — does the title/description match the user's target roles and experience level?

Example prompt/response contract:
```
Given this job listing, respond with JSON only.
{ "is_software_industry": bool, "matches_target_role": bool, "reason": str }
```

Estimated cost: ~$0.14/month at 20 jobs/day average with Haiku 4.5.

## Company Enrichment

- **Wellfound jobs**: use native company metadata (industry, funding stage)
- **All other sources**: Claude Haiku infers industry and stage from company name + job description
- Enrichment is best-effort; fields may be `null` if insufficient data
- Architecture allows swapping in Crunchbase API later without changing downstream code

## Deduplication

- **Cross-platform**: normalize `company + title` → hash as job ID; if same job appears on multiple platforms, merge into single record with all sources listed. Same company posting the same title in multiple locations is treated as one job (intentional — avoids noise)
- **Cross-day**: `state.json` stores processed job IDs; jobs seen on previous days are excluded from next run
- **Concurrent runs**: `state.json` is written atomically at end of run. If cron overlaps (run takes >24h), the second run may produce duplicates for that day — acceptable for a personal tool

## Configuration (config.yaml)

```yaml
markets:
  - tw
  - jp
  - sg

targets:
  titles:
    - "Backend Engineer"
    - "Software Engineer"
  experience_years: "3-5"
  exclude_keywords:
    - "outsourcing"
    - "派遣"

sources:
  linkedin_gmail: true
  indeed_gmail: true
  indeed_rss: true
  104: true
  cakeresume: true
  yourator: true
  wellfound: true

notification:
  to: "your@email.com"
  from: "your@email.com"
```

All settings are editable anytime via `python setup.py` or by editing the file directly. Changes take effect on the next run.

## Interactive CLI (setup.py)

On first run, guides the user through:
1. Google OAuth authentication
2. Target job titles and experience level
3. Markets to search
4. Platforms to enable/disable
5. Notification email address

Subsequent runs allow updating any setting.

## Daily Summary Email Format

Subject: `[Job Digest] 2026-03-11 — 8 new matches (TW: 5, JP: 2, SG: 1)`

Body (per job):
```
[Backend Engineer] — Stripe (SG)
Industry: fintech-infra | Stage: public
Sources: LinkedIn, Indeed
🔗 https://...
```

## Cron Setup

```bash
# Run daily at 8am
0 8 * * * cd /path/to/job-aggregator && python main.py
```

Instructions generated during setup.

## Error Handling

- Scraper failures (site changes, blocks): log warning, skip that source for the day, do not abort full run
- Gmail API errors: retry once, then abort with error email notification
- Claude API errors: retry once; if persistent, send unfiltered list with a warning note
- `state.json` corruption: rebuild from scratch (jobs may reappear once)
- Zero matches: if no jobs pass filtering, send a brief email noting "No new matches today" rather than skipping entirely, so the user knows the tool ran

## Testing

- Unit tests for parser (normalize each source format)
- Unit tests for deduplicator
- Mock Claude API responses for matcher tests
- Integration test with fixture emails and RSS samples
- No live network calls in tests

## Out of Scope (Initial Release)

- US market (addable via config)
- Company discovery / proactive new company surfacing (Phase 2)
- Web UI or dashboard
- Job application tracking
