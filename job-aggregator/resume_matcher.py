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
import google.generativeai as genai
import anthropic

from store import (
    update_resume,
    get_jobs_needing_embedding,
    get_all_jobs_with_embeddings,
    update_job_embedding,
    upsert_match,
)

_EMBED_MODEL = "models/text-embedding-004"
_TOP_N_EXPLAIN = 30


def _get_gemini():
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set")
    genai.configure(api_key=api_key)


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
    """Get Gemini text-embedding-004 vector."""
    _get_gemini()
    result = genai.embed_content(
        model=_EMBED_MODEL,
        content=text[:3000],  # model max ~2048 tokens
        task_type=task_type,
    )
    return result["embedding"]


def _explain_sync(resume_summary: str, job: dict) -> tuple[int, str]:
    """Call Claude to score fit (0-100) and write a brief explanation."""
    client = _get_claude()
    jd = (
        f"{job.get('title', '')} at {job.get('company', '')}\n"
        f"{(job.get('description') or '')[:800]}"
    )
    prompt = f"""Rate the fit between this candidate and the job. Return ONLY valid JSON:
{{"score": <0-100>, "explanation": "<1-2 specific sentences in English>"}}

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

        # 2. Embed résumé (query-style)
        resume_text = (
            f"{parsed.get('summary', '')} "
            f"Skills: {', '.join(parsed.get('skills', []))}. "
            f"Titles: {', '.join(parsed.get('titles', []))}."
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

        async def _explain_job(sim: float, job_id: str) -> None:
            job = job_map.get(job_id)
            if not job:
                return
            score, explanation = await asyncio.to_thread(_explain_sync, summary, job)
            upsert_match(resume_id, job_id, similarity=sim, score=score, explanation=explanation)

        await asyncio.gather(*[_explain_job(sim, jid) for sim, jid in top_30])

        update_resume(resume_id, status="done")

    except Exception as exc:
        update_resume(resume_id, status=f"error: {exc}")
        raise
