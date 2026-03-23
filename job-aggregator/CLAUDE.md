# Project Identity: job-aggregator
# Core Mission: Automate job seeking and resume optimization.

## 1. Mandatory Session Startup (Initialization)
Before proposing any code changes, you MUST:
- Execute `cat .agent/docs/progress.md` to understand current implementation status.
- Read `.agent/docs/task_plan.md` to sync on the technical roadmap.
- Confirm the current environment variables and API connections.

## 2. Engineering Standards
- **Data Integrity:** Since this is a job aggregator, ensure all scrapers or API calls have robust error handling (try-catch).
- **Simplification:** Before adding new logic, check if existing parsers can be reused.
- **Verification:** After any change, you MUST run the test suite (e.g., `npm test` or `pytest`).

## 3. Session Handoff & Git Protocol
When a task or session is ending, you MUST:
1. Update `.agent/docs/progress.md`.
2. Record any scraping findings or technical debt in `.agent/docs/findings.md`.
3. Update `.agent/docs/handoff.txt` with the next priority.
4. Execute Git commit: `git add . && git commit -m "[Agent] <type>: <description>"`
