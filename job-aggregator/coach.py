"""Career coach conversation module.

Implements an embedded interview coach following interview-coach-skill principles:
- Evidence-based feedback across 5 dimensions (Substance, Structure, Relevance, Credibility, Differentiation)
- Guided discovery for story management (reflective prompts, not forced STAR)
- Two conversation modes: general (stories/profile) and job-specific (decode/prep/match)
- State-aware: reads from DB (stories, culture DNA, resume) to personalise coaching
"""
import json
import re
from typing import Optional

import anthropic


# ── System prompt builder ──────────────────────────────────────────────────────

_BASE_SYSTEM = """\
You are an expert interview coach for product managers and other tech professionals.
You follow the interview-coach-skill methodology:

CORE PRINCIPLES:
1. Evidence-based: cite specific signals, not templates
2. Calibrated feedback: 5 dimensions — Substance, Structure, Relevance, Credibility, Differentiation
3. Guided discovery for stories: ask ONE reflective prompt at a time, don't demand a STAR response immediately
4. Candidate-specific: always reference the candidate's actual stories and experience, not hypotheticals
5. Actionable: end every coaching response with a clear next action

SCORING DIMENSIONS (1-5 each):
- Substance: evidence quality and depth
- Structure: narrative clarity and flow
- Relevance: question fit and focus
- Credibility: believability and proof
- Differentiation: could only THIS candidate say this? (earned secrets = 5)

STORY MANAGEMENT — when user wants to add a story:
1. Start with a reflective prompt: "Tell me about a time..." or ask about a peak experience
2. Listen for an embedded story, then help structure it as STAR
3. Extract the "earned secret" — what counterintuitive insight came out of this?
4. Score across the 5 dimensions
5. Offer to save it: respond with a JSON block at the END of your message in this exact format:
   ```save_story
   {
     "id": null,
     "title": "<3-5 word title>",
     "primary_skill": "<main skill>",
     "secondary_skill": "<secondary skill>",
     "strength": <1-5>,
     "detail": "<full STAR text + earned secret, max 400 words>"
   }
   ```

STORY IMPROVEMENT — when user wants to improve a story:
1. Ask them to walk you through it (or reference the stored version)
2. Diagnose the weakest dimension
3. Apply a minimum-viable fix with a before/after example
4. Re-score

LANGUAGE: Respond in the same language the user writes in (English or 繁體中文).
Be direct but warm. Concrete feedback over vague encouragement.
"""


def _build_context_block(stories: list[dict], culture_dna: Optional[dict],
                         resume_summary: Optional[str]) -> str:
    parts = []

    if resume_summary:
        parts.append(f"CANDIDATE PROFILE:\n{resume_summary[:800]}")

    if culture_dna:
        summary = culture_dna.get("summary", "")
        likes = ", ".join(culture_dna.get("likes", [])[:5])
        dislikes = ", ".join(culture_dna.get("dislikes", [])[:5])
        parts.append(
            f"CULTURE DNA:\nSummary: {summary}\nLikes: {likes}\nDislikes: {dislikes}"
        )

    if stories:
        story_lines = ["STORYBANK (indexed stories):"]
        for s in stories:
            story_lines.append(
                f"[{s['id']}] {s['title']} | skill: {s.get('primary_skill','')} "
                f"| strength: {s.get('strength','?')}/5"
            )
            if s.get("detail"):
                # Short excerpt for context
                excerpt = s["detail"][:200].replace("\n", " ")
                story_lines.append(f"  → {excerpt}...")
        parts.append("\n".join(story_lines))
    else:
        parts.append("STORYBANK: No stories saved yet.")

    return "\n\n".join(parts)


def _build_job_context(job: dict, culture_score: Optional[int],
                       culture_signals: Optional[dict]) -> str:
    lines = [
        f"CURRENT JOB CONTEXT:",
        f"Title: {job.get('title', '')} at {job.get('company', '')}",
        f"Location: {job.get('location', '')}",
    ]
    if job.get("description"):
        lines.append(f"\nJOB DESCRIPTION:\n{job['description'][:2500]}")
    if culture_score is not None:
        verdict = (culture_signals or {}).get("verdict", "")
        lines.append(f"\nCULTURE FIT SCORE: {culture_score}/100 — {verdict}")
    return "\n".join(lines)


# ── Main chat function ─────────────────────────────────────────────────────────

def chat(
    messages: list[dict],
    stories: list[dict],
    culture_dna: Optional[dict] = None,
    resume_summary: Optional[str] = None,
    job: Optional[dict] = None,
    culture_score: Optional[int] = None,
    culture_signals: Optional[dict] = None,
) -> tuple[str, Optional[dict]]:
    """Run one turn of the coaching conversation.

    Returns:
        (assistant_text, story_to_save_or_None)
        story_to_save is a dict if the coach wants to save a new/updated story.
    """
    client = anthropic.Anthropic()

    context = _build_context_block(stories, culture_dna, resume_summary)
    system = _BASE_SYSTEM + "\n\n" + context

    if job:
        job_context = _build_job_context(job, culture_score, culture_signals)
        system += "\n\n" + job_context

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=system,
        messages=messages,
    )

    text = response.content[0].text

    # Check if coach wants to save a story
    story_to_save = _extract_story_block(text)

    return text, story_to_save


def _extract_story_block(text: str) -> Optional[dict]:
    """Extract ```save_story ... ``` block from coach response."""
    pattern = r"```save_story\s*([\s\S]*?)```"
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except Exception:
        return None


# ── coaching_state.md import ───────────────────────────────────────────────────

def import_coaching_state(file_path: str) -> list[dict]:
    """Parse a coaching_state.md file and extract stories using Claude.

    Returns a list of story dicts ready for upsert_stories().
    """
    from pathlib import Path
    path = Path(file_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    content = path.read_text(encoding="utf-8")
    client = anthropic.Anthropic()

    prompt = f"""Parse this interview coaching state file and extract all stories from the storybank.
For each story found, return a JSON object with these fields:
- id: the story ID (e.g. "S001")
- title: story title
- primary_skill: main skill demonstrated
- secondary_skill: secondary skill (or null)
- strength: strength rating 1-5 (integer, or 3 if not specified)
- detail: full STAR story text (combine all available detail)

Return ONLY a JSON array of story objects. If a field is missing, use null.

File content:
{content[:8000]}"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    # Strip code fences
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]

    stories = json.loads(raw.strip())
    # Filter out entries without required fields
    return [s for s in stories if s.get("id") and s.get("title")]
