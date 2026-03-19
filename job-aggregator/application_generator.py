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
import difflib
import json
import os
import re as _re

import anthropic


def _candidate_name() -> str:
    """Return the candidate's name from config.yaml, falling back to 'the candidate'."""
    try:
        from config_loader import load_config
        cfg = load_config()
        return cfg.get("candidate", {}).get("name", "") or "the candidate"
    except Exception:
        return "the candidate"

# ── Resume section splitter ────────────────────────────────────────────────────

_EXPERIENCE_HEADERS = {
    "experience", "work experience", "professional experience",
    "employment", "career", "work history", "employment history",
    "career history",
}

# Plain-text section names found in real PDF resumes (no markdown prefix)
_PLAIN_SECTION_NAMES = {
    "summary", "professional summary", "profile", "objective", "about",
    "experience", "work experience", "professional experience", "employment",
    "employment history", "career", "career history", "work history",
    "education", "academic background",
    "skills", "technical skills", "core competencies", "key skills",
    "projects", "personal projects",
    "certifications", "certificates", "awards", "honors", "achievements",
    "languages", "publications", "volunteer", "activities", "interests",
}


def _detect_section(stripped: str) -> tuple[bool, str]:
    """Return (is_section, normalized_title) for a stripped line."""
    if stripped.startswith("## ") or stripped.startswith("# "):
        return True, stripped.lstrip("#").strip().lower()
    lower = stripped.lower()
    if lower in _PLAIN_SECTION_NAMES:
        return True, lower
    # All-caps short line (e.g. "WORK EXPERIENCE")
    if (2 <= len(stripped) <= 50
            and _re.match(r'^[A-Z][A-Z\s&/\-]+$', stripped)):
        return True, lower
    return False, ""


def _is_exp_title(title: str) -> bool:
    return (title in _EXPERIENCE_HEADERS
            or "experience" in title or "work" in title
            or "career" in title or "employment" in title)


def _split_resume_sections(resume_raw: str) -> tuple[str, str, str]:
    """Split a resume into (header, experience_block, rest).

    Handles both markdown-style (## EXPERIENCE) and plain-text (EXPERIENCE) headers
    as produced by PDF text extraction.

    Returns:
        header: Name + contact + any sections before EXPERIENCE (e.g. SUMMARY)
        experience_block: The EXPERIENCE section (label line + all content)
        rest: All remaining sections (SKILLS, EDUCATION, etc.)
    """
    lines = resume_raw.split("\n")
    header_lines: list[str] = []
    exp_lines: list[str] = []
    rest_lines: list[str] = []

    state = "header"  # header | exp | rest
    for line in lines:
        stripped = line.strip()
        is_section, title = _detect_section(stripped)

        if is_section:
            if _is_exp_title(title):
                state = "exp"
                exp_lines.append(line)
            elif state == "exp":
                state = "rest"
                rest_lines.append(line)
            elif state == "header":
                # Non-experience section before EXPERIENCE (SUMMARY etc.) — keep in header
                header_lines.append(line)
            else:
                rest_lines.append(line)
        else:
            if state == "header":
                header_lines.append(line)
            elif state == "exp":
                exp_lines.append(line)
            else:
                rest_lines.append(line)

    return "\n".join(header_lines), "\n".join(exp_lines), "\n".join(rest_lines)


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
                                  match_explanation: str | None = None,
                                  company_culture: dict | None = None) -> dict:
    """Generate a quick career coach brief for triage review.

    Single Claude call (haiku, ~10s). Returns structured summary with:
    headline, fit_summary, strengths, gaps, key_challenges, culture_score,
    culture_verdict, recommendation.
    """
    if not jd_text.strip():
        jd_text = "[Job description not available — assess based on job title and company name only]"

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

    company_section = ""
    if company_culture:
        cc_summary = company_culture.get("summary", "")
        cc_green = ", ".join((company_culture.get("green_flags") or [])[:3])
        cc_red = ", ".join((company_culture.get("red_flags") or [])[:3])
        cc_style = company_culture.get("work_style", "")
        company_section = f"""
Company culture profile (from web research):
- Work style: {cc_style}
- Positive signals: {cc_green}
- Concerns: {cc_red}
- Summary: {cc_summary}"""

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

Candidate background: {resume_summary[:500]}{match_section}{culture_section}{company_section}

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

def score_culture_sync(culture_dna: dict, job_title: str, company: str, jd_text: str,
                        company_culture: dict | None = None) -> dict:
    """Score a job's culture fit (0-100) and list green/yellow/red signals."""
    client = _get_claude()
    likes_txt    = "\n".join(f"- {l}" for l in culture_dna.get("likes", []))
    dislikes_txt = "\n".join(f"- {d}" for d in culture_dna.get("dislikes", []))
    green_known  = json.dumps(culture_dna.get("green_signals", []))
    red_known    = json.dumps(culture_dna.get("red_signals", []))

    company_section = ""
    if company_culture:
        cc_style = company_culture.get("work_style", "")
        cc_green = ", ".join((company_culture.get("green_flags") or [])[:3])
        cc_red = ", ".join((company_culture.get("red_flags") or [])[:3])
        company_section = f"""
Company culture (from web research):
- Work style: {cc_style}
- Known positives: {cc_green}
- Known concerns: {cc_red}"""

    prompt = f"""Score this job's culture fit for a candidate. Return ONLY valid JSON:
{{
  "score": <0-100>,
  "green": ["<positive culture signal found in this JD or company profile>"],
  "yellow": ["<neutral or ambiguous signal worth noting>"],
  "red": ["<negative culture signal found in this JD or company profile>"],
  "verdict": "<1 concise sentence culture verdict>"
}}

Candidate culture profile:
LIKES:
{likes_txt}

DISLIKES:
{dislikes_txt}

Known green signal patterns: {green_known}
Known red signal patterns: {red_known}
{company_section}
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
    """Compare JD keywords against resume across 4 ATS dimensions.

    Returns:
        present: list[str]       — keywords found in both JD and resume
        missing: list[str]       — important JD keywords absent from resume
        score: int|None          — 0-100 overall coverage (null if no JD)
        no_jd: bool              — True when JD is too short to score
        title_match: bool|None   — whether candidate title matches job title
        required_coverage: int   — % of Required Qualifications covered (0-100)
        high_freq_missing: list  — keywords appearing 2+ times in JD but not in resume
        safe: bool               — True when score >= 70
    """
    if len(jd_text.strip()) < 50:
        return {"present": [], "missing": [], "score": None, "no_jd": True,
                "title_match": None, "required_coverage": None,
                "high_freq_missing": [], "safe": False}
    client = _get_claude()
    prompt = f"""You are an ATS (Applicant Tracking System) evaluator. Analyze keyword coverage across 4 dimensions.
Return ONLY valid JSON matching this schema exactly:
{{
  "present": ["keyword found in both JD and resume — max 8, specific terms only"],
  "missing": ["important JD keyword missing from resume — max 8"],
  "high_freq_missing": ["keyword that appears 2+ times in JD but NOT in resume — max 5"],
  "title_match": true/false/null,
  "required_coverage": <integer 0-100>,
  "score": <integer 0-100>
}}

SCORING RULES:
- score: overall ATS keyword coverage. 70+ = safe zone. Weight: high_freq keywords matter 2x.
- title_match: true if candidate's most recent job title closely matches the job title being applied to.
  Use null if job title cannot be determined from JD.
- required_coverage: percentage of "Required Qualifications" bullets in the JD that the resume addresses.
  Use 100 if no Required section is found.
- high_freq_missing: keywords appearing 2+ times in the JD that are completely absent from the resume.
  These are the highest priority gaps — ATS systems weight repeated terms heavily.

WHAT TO INCLUDE:
- Tools, technologies, platforms (SQL, Amplitude, Mixpanel, A/B testing frameworks)
- Role-specific methodologies (growth loops, activation funnel, retention analysis)
- Exact phrases from Required Qualifications that are absent from resume
- Job title / seniority terms if mismatched

WHAT TO EXCLUDE:
- Generic management terms ("stakeholder", "cross-functional", "team player")
- Soft skills unless explicitly required in JD ("bilingual" is OK, "leadership" is not)

JD:
{jd_text[:2500]}

Resume:
{resume_raw[:3000]}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    result = json.loads(_strip_code_fence(msg.content[0].text))
    result["no_jd"] = False
    result["safe"] = (result.get("score") or 0) >= 70
    return result


# ── ATS-optimized resume ───────────────────────────────────────────────────────

def generate_ats_resume_sync(resume_raw: str, jd_text: str, ats_gap: dict,
                              deep_optimize: bool = False,
                              story_context: str = "") -> str:
    """Rewrite resume bullets using the interview-coach-skill methodology + ATS keyword embedding.

    Strategy: split the resume into (header, experience_block, rest) and only send the
    experience section to the LLM for rewriting. Non-experience sections (education,
    skills, contact) are reconstructed verbatim to avoid placeholder substitution.

    When deep_optimize=True, applies the full Resume Optimization Skill methodology
    with earned-secret framing and mechanism-first storytelling.
    story_context: optional newline-separated STAR story summaries from the story bank.
    """
    missing = ats_gap.get("missing", [])
    client = _get_claude()
    missing_str = ", ".join(missing) if missing else "(none — focus on quality uplift only)"

    # ── Split resume into sections ───────────────────────────────────────────────
    header_block, exp_block, rest_block = _split_resume_sections(resume_raw)

    # If we couldn't identify an experience section, fall back to full resume
    if not exp_block.strip():
        exp_block = resume_raw
        header_block = ""
        rest_block = ""

    story_block = ""
    if story_context:
        story_block = f"\nSTORY BANK (additional verified achievements you may draw from):\n{story_context}\n"

    coach_questions_instruction = ""
    if deep_optimize:
        coach_questions_instruction = """
COACH QUESTIONS — add this block ONLY if a specific metric or claim is genuinely uncertain:
## COACH QUESTIONS
- <question 1 — cite the exact bullet you're unsure about>
Max 3 questions. Omit entirely if everything is verifiable from the text above."""

    # ── Prompt philosophy ────────────────────────────────────────────────────────
    # FRAMEWORK: GraceWeng_ResumeJudgmentFramework.md
    # Priority order: Bullet quality (earned secret PM judgment) > ATS coverage
    # ATS keywords go into Skills section ONLY — never forced into bullets
    _name = _candidate_name()
    if deep_optimize:
        prompt = f"""You are {_name}'s dedicated resume coach applying their personal Resume Judgment Framework.
Your output is a submission-ready experience section — every bullet must pass ALL four checks below.

═══ BULLET STANDARD — ALL 4 REQUIRED ═══
STRUCTURE: Outcome (specific number or clear behavioral signal) + Method (what they did) + Strategic Why (why THIS decision, not another)

Checklist — reject any bullet that fails:
□ Has a specific number OR a concrete behavioral insight (not vague)
□ Shows the diagnosis before the fix ("noticed X → built Y because Z")
□ Has an earned secret: a counter-intuitive decision only someone who did this work would know
□ MAX ~25 words / 2 lines at 10pt — cut the weakest clause, never cut the number or the strategic why
□ NO em dashes (—) anywhere — use commas or semicolons instead

FAILING bullets — do not write these:
✗ "Grew DAU 6x" — result only, no method
✗ "Redesigned feed ranking to surface community norms" — method only, no result
✗ "Applied behavioral science principles" — vague, anyone could write this
✗ "Leveraged cross-functional collaboration to drive stakeholder alignment" — pure filler

STRONG bullets look like:
✓ "Grew Japan DAU 6x by redesigning feed to surface community trust signals over viral content, proving local norms outperformed global algorithmic defaults"
✓ "Cut paid acquisition 66% after diagnosing creative fatigue; shifted budget to organic repurposing and held DAU growth"
✓ "Rebuilt onboarding around Day-3 habit moment — users who read 10+ articles on Day 1 retained at 2x rate"

═══ OPENING VERBS ═══
Use only: Grew, Rebuilt, Diagnosed, Shipped, Reduced, Cut, Launched, Recovered, Negotiated, Drove, Proved, Identified, Redesigned, Reframed

═══ BULLET ORDERING (within each role) ═══
1. First bullet: strongest number + most relevant to the JD pain point
2. Second bullet: most directly relevant to JD pain point
3. Rest: descending relevance
NEVER lead with a bullet that has no number.
MAX bullets: Japan/Taiwan roles = 5, Ops/other roles = 3

═══ EMPLOYMENT GAP ═══
After the Japan role date line, if there is an employment gap to present, add this exact line in italic markdown:
*Left following HQ decision to wind down Japan market operations.*
Include this line. Do not omit or rephrase it.

═══ WHAT TO REJECT / REMOVE ═══
Delete or rewrite any bullet or phrase containing:
- "behavioral science" (unless it's the formal title of a framework she built)
- "growth hacking", "leveraged synergies", "Agile/Scrum" (unless she specifically uses this)
- "fast-paced environment", "cross-functional collaboration" (unless immediately followed by a specific outcome)
- Any detail not present in the original text below or in the story bank

═══ ATS KEYWORDS — DO NOT PUT IN BULLETS ═══
Keywords from the JD: {missing_str}
These will be added to her Skills section separately. Do NOT force them into bullets — keyword stuffing in bullets removes PM signal. Write bullets using the natural language of her actual work.
{story_block}
═══ INTEGRITY — OVERRIDES EVERYTHING ═══
- NEVER invent metrics, roles, companies, or achievements not confirmed in the source text or story bank
- If a bullet cannot be improved without fabrication, keep it EXACTLY as-is (word for word)
- Preserve all company names, job titles, date lines exactly as-is
- Output ONLY the rewritten experience section + optional COACH QUESTIONS block
- Do NOT output summary, education, skills, contact, or any other sections
{coach_questions_instruction}
═══ JD CONTEXT (what this role actually needs) ═══
{jd_text[:1500]}

Experience section to rewrite:
{exp_block[:3000]}"""
        model = "claude-sonnet-4-5"
        max_tok = 2800
    else:
        prompt = f"""You are an expert resume coach. Rewrite the experience bullets to be optimized for this job.

BULLET STRUCTURE: Outcome (number) + Method + Strategic Why — all three required.
MAX 25 words per bullet. No em dashes. Strong opening verbs only.

RULES:
- Never fabricate metrics, roles, or achievements not in the resume or story bank
- Keep bullets that can't be improved exactly as-is
- Strongest number + most relevant to JD goes FIRST in each role
- Output ONLY the experience section — no other sections, no commentary
- Preserve company names, titles, dates exactly
- Do NOT embed these JD keywords into bullets (they go in Skills section): {missing_str}
{story_block}
Job Description:
{jd_text[:1500]}

Experience section:
{exp_block[:3000]}"""
        model = "claude-haiku-4-5-20251001"
        max_tok = 2200

    msg = client.messages.create(
        model=model,
        max_tokens=max_tok,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_output = msg.content[0].text.strip()

    # ── Parse coach questions out of LLM output ──────────────────────────────────
    coach_questions: list[str] = []
    if "## COACH QUESTIONS" in raw_output:
        parts_split = raw_output.split("## COACH QUESTIONS", 1)
        rewritten_exp = parts_split[0].rstrip()
        for line in parts_split[1].split("\n"):
            line = line.strip().lstrip("-").strip()
            if line:
                coach_questions.append(line)
    else:
        rewritten_exp = raw_output

    # ── Reconstruct full resume ──────────────────────────────────────────────────
    # header (name + contact) + rewritten experience + original rest (verbatim)
    parts = []
    if header_block.strip():
        parts.append(header_block.strip())
    parts.append(rewritten_exp)
    if rest_block.strip():
        parts.append(rest_block.strip())
    full_resume = "\n\n".join(parts)

    # deep_optimize callers need coach_questions alongside the text — return a dict
    if deep_optimize:
        return {"resume": full_resume, "coach_questions": coach_questions}
    return full_resume


# ── Resume diff helper ────────────────────────────────────────────────────────

def compute_resume_diff(original: str, optimized: str) -> list[dict]:
    """Compute bullet-level diff between original and optimized resume.

    Returns list of dicts:
      {"type": "unchanged"|"changed"|"added"|"removed", "line": str, "original": str|None}
    Only compares bullet lines (starting with "-").
    """
    def _bullets(text: str) -> list[str]:
        return [l.rstrip() for l in text.split("\n") if l.strip().startswith("-")]

    orig_bullets = _bullets(original)
    new_bullets = _bullets(optimized)

    result: list[dict] = []
    sm = difflib.SequenceMatcher(None, orig_bullets, new_bullets, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for line in new_bullets[j1:j2]:
                result.append({"type": "unchanged", "line": line, "original": None})
        elif tag == "replace":
            paired = min(i2 - i1, j2 - j1)
            for k in range(paired):
                result.append({
                    "type": "changed",
                    "line": new_bullets[j1 + k],
                    "original": orig_bullets[i1 + k],
                })
            # Extra removed lines
            for k in range(paired, i2 - i1):
                result.append({"type": "removed", "line": orig_bullets[i1 + k], "original": None})
            # Extra added lines
            for k in range(paired, j2 - j1):
                result.append({"type": "added", "line": new_bullets[j1 + k], "original": None})
        elif tag == "insert":
            for line in new_bullets[j1:j2]:
                result.append({"type": "added", "line": line, "original": None})
        elif tag == "delete":
            for line in orig_bullets[i1:i2]:
                result.append({"type": "removed", "line": line, "original": None})
    return result


# ── Skills section keyword injection ─────────────────────────────────────────

def _inject_skills_keywords(rest_block: str, missing_keywords: list[str]) -> str:
    """Add missing JD keywords to the Skills section of rest_block (verbatim sections).

    Per the Resume Judgment Framework: ATS keywords belong in the Skills section,
    NOT forced into bullets. Skills section keyword weight is comparable for ATS purposes.
    Only adds keywords that Grace plausibly has (filters fabrication-risky keywords like
    "Agile/Scrum", "growth hacking", "behavioral science" as a formal methodology).

    Returns updated rest_block.
    """
    if not missing_keywords or not rest_block:
        return rest_block

    # Keywords that should never be added (fabrication risk or outdated/buzzword)
    _reject_keywords = {
        "agile", "scrum", "growth hacking", "behavioral science",
        "leveraged synergies", "growth hacking",
    }

    safe_keywords = [
        k for k in missing_keywords
        if not any(bad in k.lower() for bad in _reject_keywords)
    ]
    if not safe_keywords:
        return rest_block

    lines = rest_block.split("\n")
    result_lines = []
    in_skills = False
    skills_updated = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Detect skills section header
        if stripped.startswith("#") and any(
            kw in stripped.lower() for kw in ("skill", "technical", "tools", "core competenc")
        ):
            in_skills = True
            result_lines.append(line)
            continue

        # Detect next section header (end of skills)
        if in_skills and stripped.startswith("#") and not any(
            kw in stripped.lower() for kw in ("skill", "technical", "tools", "core competenc")
        ):
            in_skills = False
            # Inject keywords before this section header if not yet done
            if not skills_updated and safe_keywords:
                result_lines.append("- " + ", ".join(safe_keywords))
                skills_updated = True

        result_lines.append(line)

        # If still in skills and this is the last line of the block, inject after
        if in_skills and not skills_updated and i == len(lines) - 1:
            result_lines.append("- " + ", ".join(safe_keywords))
            skills_updated = True

    # If skills section found but keywords not yet added (no subsequent header)
    if in_skills and not skills_updated and safe_keywords:
        result_lines.append("- " + ", ".join(safe_keywords))

    return "\n".join(result_lines)


# ── Header rules by target market (per Resume Judgment Framework) ─────────────

def check_location_mismatch(header_block: str, jd_text: str) -> str | None:
    """Backward-compatible wrapper — calls apply_header_rules_for_market."""
    result = apply_header_rules_for_market(header_block, jd_text)
    return result.get("note")


def apply_header_rules_for_market(header_block: str, jd_text: str) -> dict:
    """Apply Resume Judgment Framework header rules based on target market.

    Framework rules (Part 5):
    - US companies: remove city/country AND phone (keep email + LinkedIn only)
    - Japan companies: KEEP address (signals regional credibility, is an advantage)
    - Singapore companies: keep by default; only remove if JD explicitly requires SG-based
    - Remote: keep everything

    Returns:
        {
            "cleaned_header": str,    # header with appropriate lines removed
            "note": str | None,       # user-visible note if something was changed
            "market": str,            # "US" | "JP" | "SG" | "TW" | "REMOTE" | "UNKNOWN"
        }
    """
    jd_lower = jd_text.lower()
    header_lower = header_block.lower()

    # ── Detect job market ───────────────────────────────────────────────────────
    is_remote = bool(_re.search(r"\bremote\b|\bwork from home\b|\bwfh\b", jd_lower))
    us_signals = ("san francisco", "new york", "seattle", "austin", "chicago",
                  "united states", "u.s.", "bay area", "los angeles", "boston",
                  "denver", "atlanta", "miami", "washington dc", "silicon valley")
    jp_signals = ("tokyo", "japan", "osaka", "日本", "東京")
    sg_signals = ("singapore",)
    eu_signals = ("london", "berlin", "amsterdam", "paris", "united kingdom",
                  "europe", "eu based", "eu-based")

    market = "UNKNOWN"
    if is_remote:
        market = "REMOTE"
    elif any(s in jd_lower for s in us_signals):
        market = "US"
    elif any(s in jd_lower for s in jp_signals):
        market = "JP"
    elif any(s in jd_lower for s in sg_signals):
        market = "SG"
    elif any(s in jd_lower for s in eu_signals):
        market = "EU"

    # ── Detect what's in header ─────────────────────────────────────────────────
    has_tw_location = any(s in header_lower for s in ("taipei", "taiwan", "新北", "台北", "台灣"))
    has_phone = bool(_re.search(r"\+\d[\d\s\-]{6,}", header_block))

    # ── Apply framework rules ───────────────────────────────────────────────────
    if market == "REMOTE" or market == "JP" or market == "UNKNOWN":
        # Keep everything — no changes needed
        return {"cleaned_header": header_block, "note": None, "market": market}

    if market == "US":
        # Remove city/country AND phone; keep email + LinkedIn only
        new_lines = []
        removed_location = False
        removed_phone = False
        for line in header_block.split("\n"):
            ll = line.lower()
            # Remove location lines
            if any(s in ll for s in ("taipei", "taiwan", "新北", "台北", "台灣",
                                      "singapore", "tokyo", "japan")) \
                    and "@" not in ll and "linkedin" not in ll and "github" not in ll:
                removed_location = True
                continue
            # Remove phone lines (lines containing +886, +65, +81, or standalone phone patterns)
            if _re.search(r"\+\d[\d\s\-]{6,}", line) and "@" not in ll and "linkedin" not in ll:
                removed_phone = True
                continue
            new_lines.append(line)

        note_parts = []
        if removed_location:
            note_parts.append("location (Taipei, Taiwan) removed — geography filtering risk for US roles")
        if removed_phone:
            note_parts.append("phone removed — unanswered +886 calls hurt more than help")
        note = ("⚠️ Header updated for US application: " + "; ".join(note_parts)
                + ". Keep only email + LinkedIn.") if note_parts else None
        return {"cleaned_header": "\n".join(new_lines), "note": note, "market": "US"}

    if market == "SG":
        # Keep by default unless JD explicitly says "must be Singapore-based"
        sg_required = bool(_re.search(
            r"must be (based in|located in|a resident of) singapore|"
            r"singapore (pr|citizen|permanent resident)|"
            r"singapore-based (only|candidates|applicants)",
            jd_lower
        ))
        if sg_required and has_tw_location:
            new_lines = [
                line for line in header_block.split("\n")
                if not (any(s in line.lower() for s in ("taipei", "taiwan", "新北", "台北", "台灣"))
                        and "@" not in line.lower() and "linkedin" not in line.lower())
            ]
            return {
                "cleaned_header": "\n".join(new_lines),
                "note": ("⚠️ JD requires Singapore-based candidates. Location removed from header. "
                         "Address this gap explicitly in your cover letter."),
                "market": "SG",
            }
        return {"cleaned_header": header_block, "note": None, "market": "SG"}

    if market == "EU":
        # Similar to US — remove TW address if present
        if has_tw_location:
            new_lines = [
                line for line in header_block.split("\n")
                if not (any(s in line.lower() for s in ("taipei", "taiwan", "新北", "台北", "台灣"))
                        and "@" not in line.lower() and "linkedin" not in line.lower())
            ]
            return {
                "cleaned_header": "\n".join(new_lines),
                "note": ("⚠️ Location removed for EU application — consider noting relocation intent in cover letter."),
                "market": "EU",
            }
        return {"cleaned_header": header_block, "note": None, "market": "EU"}

    return {"cleaned_header": header_block, "note": None, "market": market}


# ── Deep optimize entry point (used by /optimize-resume endpoint) ─────────────

def optimize_resume_deep_sync(resume_raw: str, jd_text: str, ats_gap: dict,
                               story_context: str = "") -> dict:
    """Full deep-optimization pipeline — Resume Judgment Framework applied.

    Pipeline:
      1. Apply market-specific header rules (US: strip address+phone; JP: keep)
      2. Rewrite experience section only (Sonnet, earned-secret first, no keyword stuffing)
      3. Inject missing ATS keywords into Skills section verbatim (not bullets)
      4. Reconstruct: cleaned_header + optimized_experience + updated_rest

    Returns:
        {
            "resume": str,            # submission-ready optimized resume
            "coach_questions": [...], # things optimizer couldn't verify
            "location_note": str|None,# header change warning for user
            "market": str,            # detected target market
        }
    """
    # ── Apply framework header rules ────────────────────────────────────────────
    header_block, exp_block, rest_block = _split_resume_sections(resume_raw)
    header_result = apply_header_rules_for_market(header_block, jd_text)
    cleaned_header = header_result["cleaned_header"]
    location_note = header_result["note"]
    market = header_result["market"]

    # Rebuild source with cleaned header so the LLM sees the right contact info
    source_parts = [p.strip() for p in [cleaned_header, exp_block, rest_block] if p.strip()]
    source_text = "\n\n".join(source_parts)

    # ── Rewrite experience section (earned-secret first, no keyword stuffing) ───
    deep_result = generate_ats_resume_sync(
        source_text, jd_text, ats_gap, deep_optimize=True, story_context=story_context
    )
    # deep_optimize=True returns {"resume": str, "coach_questions": list}
    result_text: str = deep_result["resume"]
    coach_questions: list[str] = deep_result.get("coach_questions", [])

    # ── Parse the result back into sections so we can update Skills separately ──
    rewritten_header, rewritten_exp, rewritten_rest = _split_resume_sections(result_text)

    # ── Inject missing keywords into Skills section (framework Part 5) ───────────
    missing_keywords = ats_gap.get("missing", [])
    if missing_keywords and rewritten_rest.strip():
        rewritten_rest = _inject_skills_keywords(rewritten_rest, missing_keywords)
    elif missing_keywords and rest_block.strip():
        # Fall back to original rest_block if rewrite didn't produce one
        rewritten_rest = _inject_skills_keywords(rest_block, missing_keywords)

    # ── Reconstruct full resume ──────────────────────────────────────────────────
    # Use rewritten header if LLM returned one, else cleaned_header from header rules
    final_header = rewritten_header.strip() or cleaned_header.strip()
    final_exp = rewritten_exp.strip()
    final_rest = rewritten_rest.strip() or rest_block.strip()

    final_parts = [p for p in [final_header, final_exp, final_rest] if p]
    final_resume = "\n\n".join(final_parts)

    # ── Career Coach review pass ──────────────────────────────────────────────────
    # Run a second independent coaching pass that evaluates each bullet from a
    # senior-recruiter "resume-write-bullets" perspective and surfaces any
    # conflicts with the framework output so the user can decide.
    coach_conflicts = _run_coach_review_sync(final_exp, jd_text)

    return {
        "resume": final_resume,
        "coach_questions": coach_questions,
        "coach_conflicts": coach_conflicts,
        "location_note": location_note,
        "market": market,
    }


def _run_coach_review_sync(experience_block: str, jd_text: str) -> list[dict]:
    """Career-coach second pass on the optimized experience block.

    Applies the resume-write-bullets skill methodology:
      - Find the real story (mechanism > outcome-only)
      - Ensure numbers are real / earned
      - Identify any bullet that could be better framed for this specific JD
      - Flag bullets that read like generic PM output vs earned-secret insights

    Returns a list of conflict items:
    [
      {
        "bullet": "<current bullet text>",
        "issue": "<what the coach sees as a problem>",
        "suggestion": "<alternative framing the coach would try>",
        "severity": "high|medium|low"   # high = likely fails recruiter scan
      },
      ...
    ]
    Returns [] if no conflicts or if parsing fails.
    """
    if not experience_block or not experience_block.strip():
        return []

    client = _get_claude()
    prompt = f"""You are a senior career coach doing a final bullet-quality review on a PM resume.
Your job is NOT to rewrite the whole resume — only flag specific bullets that have a real problem.

## WHAT TO FLAG
Flag a bullet only if it has at least one of these issues:
1. **Outcome-only**: has a number but no mechanism (reader can't tell HOW the result happened)
2. **Vague method**: describes what was done but could apply to any PM ("led cross-functional team", "applied data insights")
3. **Fabrication risk**: contains a specific number or claim that sounds invented, not earned
4. **JD mismatch**: this bullet actively hurts the application for this specific role (wrong emphasis)
5. **Em dash**: contains an em dash character (—) — these must be eliminated
6. **Too long**: bullet is clearly >25 words and would need 2+ lines at 10pt

## WHAT NOT TO FLAG
- Do not flag a bullet just because you'd phrase it differently
- Do not flag strong earned-secret bullets (specific behavioral insight + mechanism + result)
- Do not flag formatting (bold, italics, markdown structure)
- Leave bullets alone if they pass all 6 checks above

## OUTPUT FORMAT
Return ONLY valid JSON array. Each item has:
{{
  "bullet": "<exact first 12 words of the bullet>",
  "issue": "<1 sentence: specific problem>",
  "suggestion": "<1 sentence: how to fix it — do NOT rewrite the full bullet>",
  "severity": "high" | "medium" | "low"
}}

If there are no conflicts, return: []

## JD CONTEXT (what this resume needs to prove)
{jd_text[:800]}

## EXPERIENCE BLOCK TO REVIEW
{experience_block[:2500]}"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        conflicts = json.loads(raw.strip())
        if not isinstance(conflicts, list):
            return []
        # Keep only valid items
        valid = []
        for c in conflicts:
            if isinstance(c, dict) and c.get("bullet") and c.get("issue"):
                valid.append({
                    "bullet": str(c.get("bullet", ""))[:120],
                    "issue": str(c.get("issue", ""))[:200],
                    "suggestion": str(c.get("suggestion", ""))[:200],
                    "severity": c.get("severity", "medium") if c.get("severity") in ("high", "medium", "low") else "medium",
                })
        return valid
    except Exception:
        return []


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
Do not use: "I am excited to", "I would love to", "I am passionate about", "obsessing", "obsession",
"perfected", "thrilled", "honored", "humbled", "transformative", "impactful", "journey".

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
                           job_title: str, company: str, jd_text: str,
                           user_why: str = "") -> str:
    """Write a full cover letter following the 5-paragraph framework."""
    client = _get_claude()
    _name = _candidate_name()

    # Top 2 stories with full detail for paragraph selection
    story_block = ""
    for i, s in enumerate(story_matches[:2], 1):
        story_block += f"Story {i}: {s.get('bullet', '')}"
        if s.get('detail'):
            story_block += f"\n  Context: {s['detail'][:300]}"
        story_block += "\n"

    why_block = f"""
The candidate shared a genuine personal connection to this company/role:
\"\"\"{user_why.strip()}\"\"\"
Use this directly in Paragraph 5 (Why here, why now).
""" if user_why.strip() else ""

    prompt = f"""You are writing a cover letter in first person for {_name} applying for {job_title} at {company}.

Write exactly 4-5 short paragraphs following this structure. Total length: 250-350 words.

━━━ STRUCTURE ━━━

PARAGRAPH 1 — Why this company (2-3 sentences)
Start with a specific observation about {company}'s product, a decision they made, or a problem they're solving that the candidate genuinely finds interesting.
NOT: "I am excited to apply." NOT: "I admire your mission."
The hiring manager should feel this was written specifically for them.

PARAGRAPH 2 — The core story (4-6 sentences)
Tell the single most relevant story from the story bank.
Order: why they did it (motivation) → what they did (action) → what happened (result with numbers).
Do not summarize. Tell it like it happened.

PARAGRAPH 3 — The logical connection (1-2 sentences)
Connect what the candidate has done to what {company} needs for this role.
State the logic, not the conclusion. Do NOT write "therefore I believe I am the ideal candidate."

PARAGRAPH 4 — Why here, why now (2-3 sentences)
Specific reason why they want to work on this problem at {company} rather than elsewhere.
Reference product quality, culture, a specific decision they made, or a gap they see.
{why_block}
━━━ VOICE RULES ━━━
- Use contractions (I've, I'm, it's, didn't)
- Direct and opinionated, not overselling
- If a sentence sounds like it's selling, rewrite it as a factual observation
- No em dashes anywhere — use commas or periods

━━━ BANNED WORDS — never use ━━━
excited to, passionate about, strong track record, proven ability, I believe, I am confident,
I would be a great fit, leverage my experience, highly motivated, results-driven,
obsessing, obsession, perfected, thrilled, honored, humbled, synergy, impactful, journey, transformative

━━━ QUALITY CHECKS before outputting ━━━
1. Does paragraph 1 make it clear why the candidate cares about {company} specifically?
2. Is there at least one specific number or outcome in paragraph 2?
3. Could any other PM have written each sentence? If yes, make it more specific.
4. Is it under 350 words?

━━━ INPUT ━━━

Candidate background:
{resume_summary}

Most relevant stories:
{story_block}

Target role: {job_title} at {company}
JD context (what this team needs):
{jd_text[:1200]}

Output only the cover letter body — no subject line, no greeting ("Dear..."), no sign-off ("Sincerely...")."""

    msg = client.messages.create(
        model="claude-sonnet-4-5-20251001",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── Main orchestrator ──────────────────────────────────────────────────────────

async def generate_package(job_id: str, resume_id: int,
                            job: dict, resume: dict) -> dict:
    """Generate a complete application package. Returns the package dict."""
    from store import get_all_culture, get_culture_raw_text_merged, get_stories, upsert_application_package

    from company_research import get_or_research_company
    culture_rows = get_all_culture()
    stories      = get_stories()

    jd_text   = job.get("description") or ""
    if not jd_text.strip():
        jd_text = "[Job description not available — assess based on job title and company name only]"
    job_title = job.get("title") or ""
    company   = job.get("company") or ""

    # Fetch company culture profile (cached; non-blocking on failure)
    try:
        company_culture = await asyncio.to_thread(
            get_or_research_company, company, job.get("url") or ""
        )
    except Exception:
        company_culture = None

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
            score_culture_sync, merged_dna, job_title, company, jd_text, company_culture
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

    ats_resume = None
    if ats_result and resume_raw:
        ats_resume = await asyncio.to_thread(
            generate_ats_resume_sync, resume_raw, jd_text, ats_result
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
        "ats_resume":      ats_resume,
    }

    upsert_application_package(job_id, resume_id, **package)
    return package
