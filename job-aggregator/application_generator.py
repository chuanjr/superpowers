"""Application package generator — culture filter + application materials.

For each pipeline job, generates:
  1. Culture fit score & signals (vs candidate's culture DNA)
  2. Job translation (plain-language: what you'd actually do)
  3. Story matches (top 3-5 STAR stories → tailored bullets)
  4. ATS keyword gap (present vs missing)
  5. Why this company (grounded in culture alignment)
  6. Value proposition / cover letter paragraph
"""
import asyncio
import json
import os

import anthropic


def _get_claude() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _strip_code_fence(text: str) -> str:
    if "```" in text:
        parts = text.split("```")
        # Take the content block (index 1)
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


# ── Triage summary (lightweight, single-call career coach brief) ──────────────

def generate_triage_summary_sync(resume_summary: str, jd_text: str,
                                  job_title: str, company: str,
                                  culture_dna: dict | None = None,
                                  match_explanation: str | None = None) -> dict:
    """Generate a quick career coach brief for triage review.

    Single Claude call (haiku, ~10s). Returns structured summary with:
    headline, fit_summary, strengths, gaps, key_challenges, culture_score,
    culture_verdict, recommendation.
    """
    client = _get_claude()

    culture_section = ""
    if culture_dna:
        likes = ", ".join((culture_dna.get("likes") or [])[:5])
        dislikes = ", ".join((culture_dna.get("dislikes") or [])[:5])
        green = ", ".join((culture_dna.get("green_signals") or [])[:5])
        red = ", ".join((culture_dna.get("red_signals") or [])[:5])
        culture_section = f"""
Candidate culture DNA:
- Likes: {likes}
- Dislikes: {dislikes}
- Green signals (JD phrases they want to see): {green}
- Red signals (JD phrases to avoid): {red}"""

    match_section = ""
    if match_explanation:
        match_section = f"\nMatch system explanation: {match_explanation}"

    prompt = f"""You are a senior career coach reviewing a job opportunity for a candidate.
Give an honest, direct assessment. Return ONLY valid JSON:
{{
  "headline": "<8-12 words: key insight about this fit, e.g. 'Strong technical fit; culture flag on ownership model'>",
  "fit_summary": "<2-3 sentences: overall picture of fit — what aligns and what to watch for>",
  "strengths": ["<specific strength 1>", "<specific strength 2>", "<specific strength 3>"],
  "gaps": ["<specific gap 1>", "<specific gap 2>"],
  "key_challenges": ["<realistic challenge in this role for this candidate>"],
  "culture_score": <0-100 or null if no culture data>,
  "culture_verdict": "<1 sentence on culture alignment, or null if no culture data>",
  "recommendation": "<apply|consider|pass>"
}}

Scoring guidance for recommendation:
- apply: strong fit, high confidence worth applying
- consider: reasonable fit but notable concerns to weigh
- pass: significant misalignment (role type, level, culture, industry)

Candidate background: {resume_summary[:500]}{match_section}{culture_section}

Job: {job_title} at {company}
JD:
{jd_text[:2000]}"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_code_fence(msg.content[0].text))


# ── Culture DNA parsing ────────────────────────────────────────────────────────

def parse_culture_sync(raw_text: str) -> dict:
    """Extract structured culture preferences from a free-form discussion."""
    client = _get_claude()
    prompt = f"""Analyze this person's discussion about their ideal and non-ideal work environments.
Extract structured culture preferences. Return ONLY valid JSON:
{{
  "likes": ["<thing they value in a work environment>"],
  "dislikes": ["<thing they dislike or want to avoid>"],
  "green_signals": ["<phrase or pattern in a JD that signals a good culture fit>"],
  "red_signals": ["<phrase or pattern in a JD that signals a bad culture fit>"],
  "summary": "<2-3 sentence summary of their culture DNA in English>"
}}

Discussion:
{raw_text[:5000]}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_code_fence(msg.content[0].text))


# ── Culture scoring ────────────────────────────────────────────────────────────

def score_culture_sync(culture_dna: dict, job_title: str, company: str, jd_text: str) -> dict:
    """Score a job's culture fit (0-100) and list green/yellow/red signals."""
    client = _get_claude()
    likes_txt    = "\n".join(f"- {l}" for l in culture_dna.get("likes", []))
    dislikes_txt = "\n".join(f"- {d}" for d in culture_dna.get("dislikes", []))
    green_known  = json.dumps(culture_dna.get("green_signals", []))
    red_known    = json.dumps(culture_dna.get("red_signals", []))

    prompt = f"""Score this job's culture fit for a candidate. Return ONLY valid JSON:
{{
  "score": <0-100>,
  "green": ["<positive culture signal found in this JD>"],
  "yellow": ["<neutral or ambiguous signal worth noting>"],
  "red": ["<negative culture signal found in this JD>"],
  "verdict": "<1 concise sentence culture verdict>"
}}

Candidate culture profile:
LIKES:
{likes_txt}

DISLIKES:
{dislikes_txt}

Known green signal patterns: {green_known}
Known red signal patterns: {red_known}

Job: {job_title} at {company}
JD:
{jd_text[:2000]}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_code_fence(msg.content[0].text))


# ── Job translation ────────────────────────────────────────────────────────────

def translate_job_sync(job_title: str, company: str, jd_text: str) -> str:
    """Translate JD into plain, honest language about what you'd actually do."""
    client = _get_claude()
    prompt = f"""Translate this job description into plain, honest language for a senior PM candidate.

Answer: What would you actually do day-to-day? What are the real challenges? What does success look like in 6 months? What's the likely team structure / reporting line?

Write 4-5 sentences. Be direct and specific — not corporate. If the JD is vague, say so honestly.

Job: {job_title} at {company}
JD:
{jd_text[:2500]}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── Story matching ─────────────────────────────────────────────────────────────

def match_stories_sync(stories: list[dict], resume_summary: str,
                        job_title: str, company: str, jd_text: str) -> list[dict]:
    """Select top 3-5 stories and write tailored resume bullets for each."""
    client = _get_claude()
    story_list = "\n".join(
        f"[{s['id']}] {s['title']}: {(s.get('detail') or '')[:200]}"
        for s in stories
    )
    prompt = f"""You are a PM interview coach. Match this candidate's STAR stories to the job requirements.
Select the 3-5 most relevant stories and write a tailored resume bullet for each.

Return ONLY a valid JSON array:
[{{
  "story_id": "<e.g. S001>",
  "story_title": "<short title>",
  "competency": "<skill/competency this demonstrates for THIS role, 4-6 words>",
  "bullet": "<strong resume bullet: action verb + context + metric + outcome, under 25 words>"
}}]

Candidate background: {resume_summary}

Job: {job_title} at {company}
Key requirements from JD:
{jd_text[:1500]}

Available stories:
{story_list}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=900,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_code_fence(msg.content[0].text))


# ── ATS keyword check ──────────────────────────────────────────────────────────

def check_ats_sync(resume_raw: str, jd_text: str) -> dict:
    """Compare JD keywords against resume for ATS coverage."""
    client = _get_claude()
    prompt = f"""Check ATS keyword coverage between this resume and job description.
Return ONLY valid JSON:
{{
  "present": ["<important keyword found in both resume and JD>"],
  "missing": ["<important JD keyword NOT in resume — candidate should add>"],
  "score": <0-100 coverage percentage>
}}

Focus on: tools, skills, methodologies, role-specific terms. Max 8 items per list.
Only flag truly important keywords — not generic words like "product" or "team".

JD:
{jd_text[:2000]}

Resume:
{resume_raw[:3000]}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_code_fence(msg.content[0].text))


# ── Why this company ───────────────────────────────────────────────────────────

def write_why_company_sync(culture_dna: dict, culture_signals: dict,
                            job_title: str, company: str, jd_text: str) -> str:
    """Write a genuine 'why this company' answer grounded in culture alignment."""
    client = _get_claude()
    green = "\n".join(f"- {s}" for s in culture_signals.get("green", []))
    summary = culture_dna.get("summary", "")
    prompt = f"""Write a genuine, specific "why this company/role" answer for a job application.
Use the candidate's actual culture values and real signals found in the JD.
2-3 sentences. Sound like a real thoughtful person, not a template. Be specific, not generic.
Do not use phrases like "I am excited to" or "I would love to".

Candidate culture summary: {summary}

Positive culture signals found in this JD:
{green if green else "(none identified — focus on the role itself)"}

Job: {job_title} at {company}
JD context:
{jd_text[:1500]}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=280,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── Value proposition ──────────────────────────────────────────────────────────

def write_value_prop_sync(resume_summary: str, story_matches: list[dict],
                           job_title: str, company: str, jd_text: str) -> str:
    """Write a value proposition / cover letter opening paragraph."""
    client = _get_claude()
    top_bullets = "\n".join(f"- {s['bullet']}" for s in story_matches[:4])
    prompt = f"""Write a concise, punchy value proposition (cover letter opening) for this candidate.

Show specifically what they bring to THIS role — not generic strengths.
3-4 sentences. Lead with the most impressive achievement. Metric-driven. Confident but not arrogant.

Candidate background: {resume_summary}

Key achievements relevant to this role:
{top_bullets}

Target role: {job_title} at {company}
Key requirements:
{jd_text[:1000]}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=320,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── Main orchestrator ──────────────────────────────────────────────────────────

async def generate_package(job_id: str, resume_id: int,
                            job: dict, resume: dict) -> dict:
    """Generate a complete application package. Returns the package dict."""
    from store import get_all_culture, get_culture_raw_text_merged, get_stories, upsert_application_package

    culture_rows = get_all_culture()
    stories      = get_stories()

    jd_text   = job.get("description") or ""
    job_title = job.get("title") or ""
    company   = job.get("company") or ""

    parsed         = json.loads(resume.get("parsed_json") or "{}")
    resume_summary = parsed.get("summary") or ""
    resume_raw     = (resume.get("raw_text") or "")[:4000]

    # Mark as processing
    upsert_application_package(job_id, resume_id, status="processing")

    # ── Parallel phase ──────────────────────────────────────────────────────────
    async def _translate():
        return await asyncio.to_thread(translate_job_sync, job_title, company, jd_text)

    async def _ats():
        return await asyncio.to_thread(check_ats_sync, resume_raw, jd_text)

    async def _culture_score():
        if not culture_rows:
            return None, None
        # Merge parsed DNA from all entries (use latest parsed_json with content)
        merged_dna: dict = {}
        for row in culture_rows:
            if row.get("parsed_json"):
                try:
                    d = json.loads(row["parsed_json"])
                    if d and not d.get("error"):
                        # Merge lists, keep last summary
                        for key in ("likes", "dislikes", "green_signals", "red_signals"):
                            merged_dna[key] = list(dict.fromkeys(
                                merged_dna.get(key, []) + d.get(key, [])
                            ))
                        if d.get("summary"):
                            merged_dna["summary"] = d["summary"]
                except Exception:
                    pass
        if not merged_dna:
            return None, None
        signals = await asyncio.to_thread(
            score_culture_sync, merged_dna, job_title, company, jd_text
        )
        return merged_dna, signals

    async def _stories():
        if not stories or not resume_summary:
            return []
        return await asyncio.to_thread(
            match_stories_sync, stories, resume_summary, job_title, company, jd_text
        )

    translation, ats_result, culture_result, story_matches = await asyncio.gather(
        _translate(), _ats(), _culture_score(), _stories()
    )
    culture_dna, culture_signals = culture_result

    # ── Sequential phase (depends on above) ────────────────────────────────────
    why_company = None
    if culture_dna and culture_signals:
        why_company = await asyncio.to_thread(
            write_why_company_sync, culture_dna, culture_signals, job_title, company, jd_text
        )

    value_prop = None
    if story_matches:
        value_prop = await asyncio.to_thread(
            write_value_prop_sync, resume_summary, story_matches, job_title, company, jd_text
        )

    package = {
        "status":          "done",
        "job_translation": translation,
        "culture_score":   culture_signals.get("score") if culture_signals else None,
        "culture_signals": json.dumps(culture_signals) if culture_signals else None,
        "story_matches":   json.dumps(story_matches) if story_matches else None,
        "ats_gap":         json.dumps(ats_result) if ats_result else None,
        "why_company":     why_company,
        "value_prop":      value_prop,
    }

    upsert_application_package(job_id, resume_id, **package)
    return package
