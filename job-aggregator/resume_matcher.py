"""Resume PDF parsing, Gemini embedding, cosine similarity, and Claude explanation.

Flow (called as a FastAPI BackgroundTask):
  1. Claude parses raw résumé text → structured JSON
  2. Gemini embeds the résumé summary
  3. Gemini embeds any jobs that don't yet have an embedding (parallel, batch 20)
  4. Cosine similarity scores every job → stored in resume_job_matches
  5. Claude explains the top-30 matches (parallel) → stored alongside scores
"""
import asyncio
import io
import json
import math
import os

import pdfplumber
from google import genai
from google.genai import types as genai_types
import anthropic

from store import (
    update_resume,
    get_jobs_needing_embedding,
    get_all_jobs_with_embeddings,
    update_job_embedding,
    upsert_match,
    update_resume_identity,
    add_to_pipeline,
)

_EMBED_MODEL = "models/gemini-embedding-001"
_TOP_N_EXPLAIN = 30


def _get_gemini() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set")
    return genai.Client(api_key=api_key)


def _get_claude():
    return anthropic.Anthropic()


# ── PDF extraction ─────────────────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = [p.extract_text() for p in pdf.pages if p.extract_text()]
    if not pages:
        raise ValueError("Could not extract any text from the PDF")
    return "\n\n".join(pages)


# ── Sync helpers (called via asyncio.to_thread) ────────────────────────────────

def _parse_resume_sync(raw_text: str) -> dict:
    """Call Claude to extract structured info from résumé text."""
    client = _get_claude()
    prompt = f"""Extract structured information from this résumé. Return ONLY valid JSON with exactly these fields:
{{
  "name": "<full name of the candidate>",
  "headline": "<current or most recent job title, e.g. Senior Product Manager>",
  "skills": ["skill1", "skill2"],
  "titles": ["title1", "title2"],
  "years_experience": <integer>,
  "industries": ["industry1"],
  "summary": "<2-3 sentence background summary in English>"
}}

Résumé text:
{raw_text[:4000]}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    # Strip markdown code fences if present
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _embed_sync(text: str, task_type: str) -> list[float]:
    """Get Gemini embedding vector."""
    client = _get_gemini()
    result = client.models.embed_content(
        model=_EMBED_MODEL,
        contents=text[:3000],
        config=genai_types.EmbedContentConfig(task_type=task_type),
    )
    return result.embeddings[0].values


def _explain_sync(resume_summary: str, job: dict, candidate_industries: list[str] | None = None,
                  feedback_context: str | None = None,
                  resume_id: int | None = None) -> tuple[int, str]:
    """Call Claude to score fit (0-100) and write a brief explanation."""
    client = _get_claude()
    jd = (
        f"{job.get('title', '')} at {job.get('company', '')}\n"
        f"{(job.get('description') or '')[:800]}"
    )
    industries_hint = ""
    if candidate_industries:
        industries_hint = f"\nCandidate's target industries: {', '.join(candidate_industries)}. Significantly penalize (score below 40) jobs in unrelated industries if the candidate has no background there."
    feedback_hint = ""
    if feedback_context:
        feedback_hint = f"\n\nIMPORTANT — User feedback on a previous score for this job: {feedback_context}\nAdjust your score accordingly."

    # Include recent pass patterns to improve future scoring accuracy
    pass_hint = ""
    if resume_id:
        from store import get_recent_feedback
        recent_passes = get_recent_feedback(resume_id, rating="down", limit=10)
        if recent_passes:
            reasons = [r["reason"] for r in recent_passes if r.get("reason")]
            if reasons:
                reason_counts: dict[str, int] = {}
                for r in reasons:
                    reason_counts[r] = reason_counts.get(r, 0) + 1
                reason_summary = "; ".join(
                    f"{r} (×{c})" if c > 1 else r
                    for r, c in sorted(reason_counts.items(), key=lambda x: -x[1])
                )
                pass_hint = (
                    f"\n\nRECENT PASS PATTERNS (last {len(recent_passes)} passes): "
                    f"This candidate passed on jobs for these reasons: {reason_summary}. "
                    "If this job has similar characteristics, score it lower (under 50)."
                )

    prompt = f"""Rate the fit between this candidate and the job. Return ONLY valid JSON:
{{"score": <0-100>, "explanation": "<1-2 specific sentences in English>"}}

IMPORTANT SCORING RULES:
- If the job is for physical/offline goods (signals: 供應鏈, 採購, 庫存, supply chain, procurement, inventory, merchandise, 商品, physical retail, F&B, manufacturing, logistics), and the candidate is a tech/software/digital PM with no such background, score it below 35.
- If the job title says "Product Manager" but the actual role is supply chain, procurement, or physical merchandise management, treat it as a non-tech role.
- Only score high (70+) if both the role type AND industry match the candidate's background.{industries_hint}{feedback_hint}{pass_hint}

Candidate background:
{resume_summary[:500]}

Job:
{jd}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text.strip())
    return int(data["score"]), str(data["explanation"])


# ── Math ───────────────────────────────────────────────────────────────────────

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


# ── Main orchestration (runs as a background task) ─────────────────────────────

async def process_matching(resume_id: int, raw_text: str) -> None:
    """Parse résumé, embed everything, score all jobs, explain top 30."""
    try:
        update_resume(resume_id, status="processing")

        # 1. Parse résumé with Claude
        parsed = await asyncio.to_thread(_parse_resume_sync, raw_text)
        update_resume(resume_id, parsed_json=json.dumps(parsed))
        if parsed.get("name"):
            update_resume_identity(resume_id, parsed["name"], parsed.get("headline", ""))

        # 2. Embed résumé (query-style)
        resume_text = (
            f"{parsed.get('summary', '')} "
            f"Skills: {', '.join(parsed.get('skills', []))}. "
            f"Titles: {', '.join(parsed.get('titles', []))}. "
            f"Industries: {', '.join(parsed.get('industries', []))}."
        )
        resume_emb = await asyncio.to_thread(_embed_sync, resume_text, "retrieval_query")
        update_resume(resume_id, embedding=json.dumps(resume_emb))

        # 3. Embed jobs that don't have an embedding yet (parallel, batch of 20)
        jobs_needing_emb = get_jobs_needing_embedding()

        async def _embed_job(job: dict) -> None:
            text = (
                f"{job['title']} at {job.get('company', '')}. "
                f"{(job.get('description') or '')[:1000]}"
            )
            emb = await asyncio.to_thread(_embed_sync, text, "retrieval_document")
            update_job_embedding(job["id"], json.dumps(emb))

        for i in range(0, len(jobs_needing_emb), 20):
            await asyncio.gather(*[_embed_job(j) for j in jobs_needing_emb[i:i + 20]])

        # 4. Cosine similarity for every embedded job → store in DB
        all_job_embs = get_all_jobs_with_embeddings()
        scored: list[tuple[float, str]] = []  # (similarity, job_id)
        for row in all_job_embs:
            job_emb = json.loads(row["embedding"])
            sim = cosine_similarity(resume_emb, job_emb)
            upsert_match(resume_id, row["id"], similarity=sim)
            scored.append((sim, row["id"]))

        # 5. Claude explanations for top-30 (parallel)
        scored.sort(reverse=True, key=lambda x: x[0])
        top_30 = scored[:_TOP_N_EXPLAIN]

        # We need job metadata for explanations — fetch from the already-loaded rows
        # Build a quick lookup from the embeddings query (only has id).
        # We need title/company/description — re-fetch those.
        from store import get_all_jobs  # local import to avoid circular at module level
        job_map = {j["id"]: j for j in get_all_jobs()}

        summary = parsed.get("summary", raw_text[:300])
        candidate_industries = parsed.get("industries", [])

        async def _explain_job(sim: float, job_id: str) -> None:
            job = job_map.get(job_id)
            if not job:
                return
            score, explanation = await asyncio.to_thread(
                _explain_sync, summary, job, candidate_industries, None, resume_id
            )
            upsert_match(resume_id, job_id, similarity=sim, score=score, explanation=explanation)

        for sim, jid in top_30:
            await _explain_job(sim, jid)

        # Auto-add top matches (score >= 70) to pipeline as "recommended"
        from store import get_matches as _get_matches
        for match in _get_matches(resume_id):
            if (match.get("score") or 0) >= 70:
                add_to_pipeline(match["job_id"], resume_id=resume_id,
                                status="triage", verdict=None)

        update_resume(resume_id, status="done")

    except Exception as exc:
        update_resume(resume_id, status=f"error: {exc}")
        raise


# ── Single-job match (for URL imports) ────────────────────────────────────────

async def process_single_job_match(resume_id: int, job_id: str) -> None:
    """Embed + score a single newly-imported job against an existing resume."""
    from store import get_resume, get_job, upsert_match

    resume = get_resume(resume_id)
    job    = get_job(job_id)
    if not resume or not job:
        return

    parsed = json.loads(resume.get("parsed_json") or "{}")
    resume_emb = json.loads(resume.get("embedding") or "null")
    if not resume_emb:
        return

    # Embed the new job
    job_text = f"{job['title']} at {job.get('company', '')}. {(job.get('description') or '')[:1000]}"
    job_emb  = await asyncio.to_thread(_embed_sync, job_text, "retrieval_document")
    update_job_embedding(job_id, json.dumps(job_emb))

    sim = cosine_similarity(resume_emb, job_emb)
    upsert_match(resume_id, job_id, similarity=sim)

    # Score + explain
    summary    = parsed.get("summary", "")
    industries = parsed.get("industries", [])
    score, explanation = await asyncio.to_thread(_explain_sync, summary, job, industries)
    upsert_match(resume_id, job_id, similarity=sim, score=score, explanation=explanation)

    # Auto-add to review queue if high score
    if score >= 70:
        add_to_pipeline(job_id, resume_id=resume_id, status="triage", verdict=None)


# ── Feedback-driven re-scoring ─────────────────────────────────────────────────

async def rescore_with_feedback(resume_id: int, job_id: str,
                                 rating: str, reason: str | None) -> tuple[int, str]:
    """Re-score a single job for a resume, incorporating user feedback."""
    from store import get_resume, get_job, get_matches, upsert_match

    resume = get_resume(resume_id)
    if not resume:
        raise ValueError(f"Resume {resume_id} not found")
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")

    parsed = json.loads(resume.get("parsed_json") or "{}")
    summary = parsed.get("summary", "")
    industries = parsed.get("industries", [])

    direction = "too high" if rating == "down" else "too low" if rating == "up_wrong" else "appropriate"
    feedback_ctx = f"Score was {direction}."
    if reason:
        feedback_ctx += f" Reason: {reason}"

    score, explanation = await asyncio.to_thread(
        _explain_sync, summary, job, industries, feedback_ctx
    )

    # Preserve existing similarity
    existing = next((m for m in get_matches(resume_id) if m["job_id"] == job_id), None)
    sim = existing["similarity"] if existing else 0.0
    upsert_match(resume_id, job_id, similarity=sim, score=score, explanation=explanation)
    return score, explanation
