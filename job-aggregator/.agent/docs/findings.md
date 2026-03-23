# Technical Findings & Debt

Last updated: 2026-03-23

## Scraping

### 104 Jobs (`fetchers/scraper.py`)
- Company names include industry suffixes ("股份有限公司", "有限公司") — stripped via regex normalization in scraper
- Playwright browser auth required; session state in `credentials/cakeresume_state.json`
- Must install separately: `playwright install chromium`

### Gmail Parser (`fetchers/gmail_parser.py`)
- LinkedIn alert emails have varying HTML structure across regions — parser handles multiple template variants
- Indeed alerts use table-based layout; extract job rows by `tr[data-jk]` selector

### CakeResume (`fetchers/scraper.py`)
- Requires authenticated Playwright session
- Auth flow: `setup_cakeresume_auth.py` opens browser for login, saves cookies to `credentials/cakeresume_state.json`

## Technical Debt

### Model Versioning
- Anthropic SDK model names change with releases. Currently pinned to `claude-sonnet-4-6`.
- If "model not found" 404 errors recur, update both occurrences in `application_generator.py` (lines ~518, ~1086).

### Hardcoded Candidate Name (resolved)
- Previously hardcoded "Grace Weng" in two prompt locations in `application_generator.py`
- Now reads from `config.yaml` → `candidate.name` via `_candidate_name()` helper function

### Function Naming (resolved)
- `renderPkgDrawer` was renamed to `loadPackage` but a stale call remained in resume upload handler
- Caused silent "upload failed" errors despite 200 OK — fixed in commit 2a423e7

### Gmail Token Refresh (resolved)
- Server previously used in-memory `_refresh_state` dict; reset on every server restart
- Now reads actual `credentials/token.json` and refreshes via `google.oauth2.credentials.Credentials`
- `GET /api/gmail/status` endpoint called on page load to avoid stale banner state

### Culture Score Table Column (resolved)
- Pipeline table had a Culture column showing emoji dots (⚫🟢🟡🔴) but no interaction
- Removed from table header and rows — culture data still accessible in package drawer via "🎭 Culture Check"

## Dependencies to Watch

- `httpx`: pin to `>=0.23,<0.28` — version 0.28+ breaks Anthropic SDK streaming internals
- `pdfplumber`: PDF text extraction; quality degrades on scanned/image PDFs (no OCR support)
- `google-genai`: Gemini embedding SDK — separate package from `google-auth`/`google-api-python-client`
- `reportlab`: PDF generation for resume download feature
