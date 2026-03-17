"""FastAPI entrypoint for TeachBack."""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict
import contextlib
import inspect
import json
import logging
import mimetypes
import os
import re
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from google.cloud import storage as gcs_storage
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from server.gemini_session import (
    create_correction_session,
    create_live_session,
    extract_from_response,
    send_audio_chunk,
    send_text_instruction,
    send_tool_response,
)
from server.material_parser import generate_topic_material, maybe_summarize_material, normalize_text, parse_material
from server.modes import MODES, list_modes
from server.personas import PERSONAS, list_personas
from server.scoring import SCORING_FALLBACK_PROMPT, SCORE_FUNCTION, WRAP_UP_PROMPT
from server.trails_store import TrailDocumentRecord, TrailRecord, TrailStore

load_dotenv()

LOGGER = logging.getLogger("teachback")
logging.basicConfig(level=logging.INFO)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SESSION_TTL = timedelta(minutes=15)
STOP_TOOL_TIMEOUT_SECONDS = 8
WRAP_UP_WAIT_SECONDS = 4
ISSUE_DETECTION_MODEL = "gemini-2.5-flash"
ISSUE_DETECTION_MIN_WORDS = 8
ISSUE_DETECTION_COOLDOWN = timedelta(seconds=18)
ISSUE_DETECTION_TIMEOUT_SECONDS = 4
ISSUE_DETECTION_MATERIAL_CHARS = 3200
CORRECTION_VERIFICATION_MODEL = "gemini-2.5-flash"
CORRECTION_VERIFICATION_TIMEOUT_SECONDS = 3
CORRECTION_TIMEOUT_SECONDS = 60
CORRECTION_HANDOFF_LINE = "Correct. That's the key idea. I'm handing you back to the main tutor now."
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TRAILS_DB_PATH = Path(os.getenv("TEACHBACK_DB_PATH", STATIC_DIR.parent / "data" / "teachback.db"))
PRESET_TRAILS_DIR = Path(os.getenv("PRESET_TRAILS_DIR", STATIC_DIR.parent / "seed_documents"))
PRESET_TRAILS_GCS_BUCKET = os.getenv("PRESET_TRAILS_GCS_BUCKET", "").strip()
PRESET_TRAILS_GCS_PREFIX = os.getenv("PRESET_TRAILS_GCS_PREFIX", "").strip().strip("/")
PRESET_TRAILS_AUTO_SYNC = os.getenv("PRESET_TRAILS_AUTO_SYNC", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PRECOMPUTED_TEXT_ARTIFACTS = ("material.txt", "prepared.txt")
PRECOMPUTED_JSON_ARTIFACT = "prepared.json"


class TopicPayload(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=4000)


class TrailCreatePayload(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=1000)


@dataclass
class SourceDocument:
    name: str
    label: str
    mime_type: str


class ScorePayload(BaseModel):
    accuracy_score: int
    completeness_score: int
    clarity_score: int
    depth_score: int
    overall_score: int
    strengths: list[str]
    gaps: list[str]
    misconceptions: list[str]
    next_steps: list[str]


@dataclass
class SessionRecord:
    session_id: str
    material: str
    material_preview: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RuntimeState:
    session_id: str
    mode_id: str
    persona_id: str
    material: str
    orchestrator_state: str = "main_active"
    transcript: list[dict[str, str]] = field(default_factory=list)
    correction_transcript: list[dict[str, str]] = field(default_factory=list)
    tool_called: asyncio.Event = field(default_factory=asyncio.Event)
    session_closed: asyncio.Event = field(default_factory=asyncio.Event)
    scores: dict[str, Any] | None = None
    last_user_text: str = ""
    last_agent_text: str = ""
    partial_user_text: str = ""
    partial_agent_text: str = ""
    correction_last_user_text: str = ""
    correction_last_agent_text: str = ""
    correction_partial_user_text: str = ""
    correction_partial_agent_text: str = ""
    close_task: asyncio.Task | None = None
    issue_detection_task: asyncio.Task | None = None
    correction_completion_task: asyncio.Task | None = None
    correction_verification_task: asyncio.Task | None = None
    correction_timeout_task: asyncio.Task | None = None
    interruption_active: bool = False
    last_issue_signature: str = ""
    last_issue_at: datetime | None = None
    pending_issue_signature: str = ""
    pending_issue_claim: str = ""
    pending_issue_expected_correction: str = ""
    correction_closing_started: bool = False
    correction_closing_transcript_seen: bool = False
    resolved_issue_signatures: set[str] = field(default_factory=set)
    resolved_issue_claims: list[str] = field(default_factory=list)


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = asyncio.Lock()

    async def set(self, record: SessionRecord):
        async with self._lock:
            self._sessions[record.session_id] = record
            self._prune_locked()

    async def get(self, session_id: str) -> SessionRecord | None:
        async with self._lock:
            self._prune_locked()
            return self._sessions.get(session_id)

    async def delete(self, session_id: str):
        async with self._lock:
            self._sessions.pop(session_id, None)

    def _prune_locked(self):
        cutoff = datetime.now(timezone.utc) - SESSION_TTL
        expired = [
            session_id
            for session_id, record in self._sessions.items()
            if record.created_at < cutoff
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)


def build_client():
    api_key = os.getenv("GOOGLE_API_KEY")
    return genai.Client(api_key=api_key) if api_key else genai.Client()


def build_gcs_client():
    if not PRESET_TRAILS_GCS_BUCKET:
        return None
    try:
        return gcs_storage.Client()
    except Exception as exc:  # pragma: no cover - depends on local/cloud auth state
        LOGGER.warning("Could not initialize GCS client for preset trails: %s", exc)
        return None


def preview_text(material: str) -> str:
    compact = material.replace("\n", " ").strip()
    return compact[:200] + ("..." if len(compact) > 200 else "")


def build_session_payload(record: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "material_preview": record.material_preview,
        "material_text": record.material,
    }


def safe_document_name(filename: str) -> str | None:
    name = Path(filename).name
    if not name or name != filename or "/" in filename or "\\" in filename:
        return None
    return name


def pdf_label_from_filename(filename: str) -> str:
    stem = Path(filename).stem.replace("_", " ").replace("-", " ").strip()
    return stem.title() or filename


def build_learning_goals_text(trail: TrailRecord) -> str:
    seed_key = (trail.seed_key or "").strip()
    templates = {
        "software_engineering_interview_pack": """
Learning goals

Session objective:
- Help the learner explain core interview concepts clearly enough to defend them out loud.

Must cover:
- Why binary search depends on sorted input before any half-elimination step is valid.
- The difference between time complexity, space complexity, and practical tradeoffs.
- At least one data-structure choice and why it fits a specific use case.
- A simple system-design tradeoff such as latency vs throughput or consistency vs complexity.

Checkpoint cues:
- Ask the learner to explain Big O in plain language instead of only naming symbols.
- Ask for one concrete example where a hash map is a better fit than a list scan.
- Ask the learner to connect an algorithm choice to a real interview scenario.

Misconceptions to catch:
- "Binary search works on any list."
- "Big O is the exact runtime."
- "More data structures is always better than the simplest working choice."

Success signal:
- The learner explains the condition, mechanism, and tradeoff behind at least one core concept without hand-waving.
""",
        "machine_learning_fundamentals": """
Learning goals

Session objective:
- Help the learner explain how a basic supervised-learning pipeline works from data to evaluation.

Must cover:
- The difference between training, validation, and test data.
- Bias vs variance and why both matter.
- Why regularization helps control overfitting.
- One meaningful evaluation metric and when to use it.

Checkpoint cues:
- Ask the learner to explain overfitting without relying on buzzwords.
- Ask what changes when the task is classification instead of regression.
- Ask for one example of a model doing well on training data but poorly on unseen data.

Misconceptions to catch:
- "Higher training accuracy always means the model is better."
- "Bias is always bad and variance is always good."
- "Regularization just makes the model more complicated."

Success signal:
- The learner can relate model behavior, generalization, and evaluation back to the data split clearly.
""",
        "product_sense_and_mvp_strategy": """
Learning goals

Session objective:
- Help the learner reason from user pain to an MVP decision with clear product tradeoffs.

Must cover:
- The user problem being solved and why it matters.
- What an MVP includes versus what it intentionally leaves out.
- How success would be measured after launch.
- One concrete tradeoff between speed, scope, and user value.

Checkpoint cues:
- Ask the learner to identify the riskiest assumption in the product idea.
- Ask what should be cut first if the team only had one week to ship.
- Ask how they would tell whether users actually got value from the MVP.

Misconceptions to catch:
- "An MVP should include every feature users asked for."
- "More polished design always matters more than solving the pain point."
- "If users like the idea in theory, the product already has product-market fit."

Success signal:
- The learner can defend a narrow MVP that still tests the core value proposition.
""",
        "personal_finance_basics": """
Learning goals

Session objective:
- Help the learner connect everyday money habits to long-term financial stability.

Must cover:
- The purpose of a budget and what it actually tracks.
- Why emergency savings matter before bigger investing goals.
- How interest works for savings versus debt.
- One important credit-health behavior such as on-time payments or utilization.

Checkpoint cues:
- Ask the learner to explain the difference between needs, wants, and fixed obligations.
- Ask why minimum payments can keep someone in debt for a long time.
- Ask for one realistic first step for someone with no savings buffer.

Misconceptions to catch:
- "Budgeting means you can never spend on fun."
- "A credit card balance helps your score more than paying it down."
- "Saving can wait until debt disappears entirely in every case."

Success signal:
- The learner can explain at least one practical decision they would make differently after the session.
""",
        "climate_change_and_energy_basics": """
Learning goals

Session objective:
- Help the learner explain climate basics and energy tradeoffs without collapsing everything into slogans.

Must cover:
- The difference between weather, climate, and long-term trends.
- How greenhouse gases affect warming.
- Why energy systems involve tradeoffs between reliability, cost, and emissions.
- One example of how renewable energy helps and one challenge that still has to be managed.

Checkpoint cues:
- Ask the learner to explain why a single cold day does not disprove climate change.
- Ask what changes on the grid when renewable generation grows.
- Ask for one policy or infrastructure challenge that exists even when the science is clear.

Misconceptions to catch:
- "Weather and climate are basically the same thing."
- "Renewables solve the whole energy problem with no tradeoffs."
- "If emissions are invisible, they are not measurable."

Success signal:
- The learner can explain both the science and the energy-system tradeoffs in the same answer.
""",
    }
    template = templates.get(seed_key)
    if template:
        return normalize_text(template)

    title = trail.title.strip() or "this study trail"
    description = trail.description.strip() or "the selected source material"
    return normalize_text(
        f"""
Learning goals

Session objective:
- Help the learner build a confident spoken explanation of {title}.

Must cover:
- The central ideas in {description}.
- At least one mechanism, example, or tradeoff from the source material.
- One point where the learner has to move beyond memorized wording.

Checkpoint cues:
- Ask the learner to restate a key concept in plain language.
- Ask what idea connects the most important concepts together.
- Ask for one specific example that proves the explanation is grounded.

Success signal:
- The learner can explain the material clearly enough that another person could follow it.
"""
    )


def build_transcript_text(messages: list[dict[str, str]]) -> str:
    return "\n".join(f"{item['speaker']}: {item['text']}" for item in messages if item["text"].strip())


def count_words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def issue_signature(text: str) -> str:
    return normalize_text(text).lower()


def recent_transcript_excerpt(messages: list[dict[str, str]], limit: int = 6) -> str:
    recent = messages[-limit:]
    return "\n".join(f"{item['speaker']}: {item['text']}" for item in recent if item["text"].strip())


def resolved_issues_excerpt(claims: list[str], limit: int = 5) -> str:
    recent = [claim for claim in claims[-limit:] if claim.strip()]
    return "\n".join(f"- {claim}" for claim in recent)


def compact_grounding_excerpt(material: str, max_chars: int = ISSUE_DETECTION_MATERIAL_CHARS) -> str:
    return normalize_text(material)[:max_chars]


def parse_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None

    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw[start : end + 1])

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


async def detect_learning_issue(
    client,
    material: str,
    transcript_excerpt: str,
    latest_user_text: str,
    resolved_issue_context: str = "",
) -> dict[str, Any] | None:
    if count_words(latest_user_text) < ISSUE_DETECTION_MIN_WORDS:
        LOGGER.info(
            "Issue detection skipped for short turn (%d words): %s",
            count_words(latest_user_text),
            latest_user_text[:160],
        )
        return None

    LOGGER.info("Issue detection evaluating latest user turn: %s", latest_user_text[:240])

    prompt = f"""
You are reviewing a live tutoring session and deciding whether the latest user turn contains a factual mistake or misconception that should be corrected right now.

Only flag an issue when all of the following are true:
- the user made a concrete knowledge claim
- the claim is meaningfully wrong or misleading
- correcting it now would improve the lesson

Do not flag:
- harmless simplifications
- incomplete thoughts
- open questions
- statements that are not clearly wrong

Already resolved misconceptions in this session:
{resolved_issue_context or "None recorded yet."}

Do not flag one of those already-resolved misconceptions again unless the latest user turn clearly reasserts the same incorrect claim after the correction.

Return JSON only with this exact shape:
{{
  "flag": false,
  "claim": "",
  "cue": "",
  "prompt": "",
  "suggested_correction": "",
  "confidence": 0.0
}}

If you set "flag" to true:
- "claim" should name the mistaken claim in one sentence
- "cue" should be a short UI line explaining why the claim matters
- "prompt" should ask the learner to restate the corrected idea
- "suggested_correction" should be a concise correction the sidecar can preload
- "confidence" should be between 0 and 1

Reference material excerpt:
{compact_grounding_excerpt(material)}

Recent conversation:
{transcript_excerpt or "No recent transcript yet."}

Latest user turn:
{latest_user_text}
""".strip()

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=ISSUE_DETECTION_MODEL,
                contents=prompt,
            ),
            timeout=ISSUE_DETECTION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        LOGGER.warning("Issue detection timed out for latest turn: %s", latest_user_text[:120])
        return None
    except Exception as exc:  # pragma: no cover - external API behavior
        LOGGER.warning("Issue detection failed: %s", exc)
        return None

    raw_response_text = (response.text or "").strip()
    LOGGER.info("Issue detection raw response: %s", raw_response_text[:500])
    payload = parse_json_object(raw_response_text)
    if not payload:
        LOGGER.warning("Issue detection returned unparsable JSON for turn: %s", latest_user_text[:160])
        return None
    if not payload.get("flag"):
        LOGGER.info("Issue detection decided not to flag this turn.")
        return None

    confidence = float(payload.get("confidence") or 0)
    claim = normalize_text(str(payload.get("claim") or ""))
    cue = normalize_text(str(payload.get("cue") or ""))
    prompt_text = normalize_text(str(payload.get("prompt") or ""))
    suggested_correction = normalize_text(str(payload.get("suggested_correction") or ""))

    if confidence < 0.72 or not claim or not suggested_correction:
        LOGGER.info(
            "Issue detection rejected candidate: confidence=%.2f claim=%s suggested=%s",
            confidence,
            claim or "<empty>",
            "yes" if suggested_correction else "no",
        )
        return None

    LOGGER.info("Issue detection flagged claim: %s (confidence=%.2f)", claim, confidence)
    return {
        "claim": claim,
        "cue": cue or f"Potential misconception flagged: {claim}",
        "prompt": prompt_text
        or f"I paused the lesson because I caught a likely misconception: “{claim}.” Can you restate the corrected idea clearly?",
        "suggestedCorrection": suggested_correction,
        "confidence": confidence,
    }


async def verify_correction_turn(
    client,
    issue_claim: str,
    expected_correction: str,
    learner_turn: str,
    transcript_excerpt: str,
) -> dict[str, Any] | None:
    learner_turn = normalize_text(learner_turn)
    if count_words(learner_turn) < ISSUE_DETECTION_MIN_WORDS:
        return None

    prompt = f"""
You are checking whether a learner has correctly restated a corrected idea during a live tutoring interruption.

Return JSON only with this exact shape:
{{
  "accepted": false,
  "resolved_summary": "",
  "confidence": 0.0
}}

Accept the learner's response when it clearly captures the corrected idea, even if the wording is not identical.
Reject it if it is still wrong, still vague, or misses the key condition needed to fix the misconception.

Misconception being corrected:
{issue_claim}

Expected corrected idea:
{expected_correction}

Recent correction transcript:
{transcript_excerpt or "No correction transcript yet."}

Latest learner correction turn:
{learner_turn}
""".strip()

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=CORRECTION_VERIFICATION_MODEL,
                contents=prompt,
            ),
            timeout=CORRECTION_VERIFICATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        LOGGER.warning("Correction verification timed out for turn: %s", learner_turn[:160])
        return None
    except Exception as exc:  # pragma: no cover - external API behavior
        LOGGER.warning("Correction verification failed: %s", exc)
        return None

    raw_response_text = (response.text or "").strip()
    LOGGER.info("Correction verification raw response: %s", raw_response_text[:500])
    payload = parse_json_object(raw_response_text)
    if not payload:
        LOGGER.warning("Correction verification returned unparsable JSON for turn: %s", learner_turn[:160])
        return None

    accepted = bool(payload.get("accepted"))
    resolved_summary = normalize_text(str(payload.get("resolved_summary") or ""))
    try:
        confidence = float(payload.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0

    if not accepted or confidence < 0.72:
        LOGGER.info(
            "Correction verifier did not accept learner turn: accepted=%s confidence=%.2f",
            accepted,
            confidence,
        )
        return None

    return {
        "resolved_summary": resolved_summary or expected_correction,
        "confidence": max(0.0, min(confidence, 1.0)),
    }


def schedule_issue_detection(websocket: WebSocket, client, state: RuntimeState, latest_user_text: str):
    latest_user_text = normalize_text(latest_user_text)
    if not latest_user_text:
        return
    if state.session_closed.is_set() or state.tool_called.is_set() or state.interruption_active:
        LOGGER.info(
            "Issue detection not scheduled (closed=%s tool_called=%s interruption=%s) for turn: %s",
            state.session_closed.is_set(),
            state.tool_called.is_set(),
            state.interruption_active,
            latest_user_text[:160],
        )
        return

    if state.issue_detection_task and not state.issue_detection_task.done():
        LOGGER.info("Cancelling previous issue detection task before scheduling a new one.")
        state.issue_detection_task.cancel()

    async def _run():
        try:
            issue = await detect_learning_issue(
                client,
                state.material,
                recent_transcript_excerpt(state.transcript, limit=4),
                latest_user_text,
                resolved_issues_excerpt(state.resolved_issue_claims),
            )
        except asyncio.CancelledError:
            LOGGER.info("Issue detection task cancelled before completion.")
            return

        if not issue or state.session_closed.is_set() or state.interruption_active:
            LOGGER.info(
                "Issue detection produced no actionable signal (issue=%s closed=%s interruption=%s).",
                bool(issue),
                state.session_closed.is_set(),
                state.interruption_active,
            )
            return

        signature = issue_signature(issue.get("claim") or issue.get("suggestedCorrection") or "")
        if signature and signature in state.resolved_issue_signatures:
            LOGGER.info("Issue detection suppressed already-resolved claim: %s", issue.get("claim") or signature)
            return
        now = datetime.now(timezone.utc)
        if (
            signature
            and state.last_issue_signature == signature
            and state.last_issue_at is not None
            and now - state.last_issue_at < ISSUE_DETECTION_COOLDOWN
        ):
            LOGGER.info("Issue detection suppressed duplicate claim during cooldown: %s", issue.get("claim") or signature)
            return

        state.last_issue_signature = signature
        state.last_issue_at = now
        state.pending_issue_signature = signature
        state.pending_issue_claim = issue.get("claim") or ""
        state.orchestrator_state = "correction_signaled"
        LOGGER.info(
            "Emitting correction_signal for session %s: claim=%s confidence=%s",
            state.session_id,
            issue.get("claim"),
            issue.get("confidence"),
        )
        await websocket.send_json({"type": "correction_signal", "data": issue})

    state.issue_detection_task = asyncio.create_task(_run())


async def prepare_material_input(
    client,
    file: UploadFile | None = None,
    text: str | None = None,
) -> str:
    if file is None and not (text or "").strip():
        raise HTTPException(status_code=400, detail="Provide a file or pasted text.")

    if (text or "").strip():
        material = await maybe_summarize_material(client, normalize_text(text or ""))
    else:
        file_bytes = await file.read()
        material = await prepare_material_bytes(
            client,
            file_bytes,
            file.filename or "upload",
            file.content_type or "",
        )

    if not material:
        raise HTTPException(status_code=400, detail="Could not prepare study material from that input.")

    return material


async def prepare_material_bytes(client, file_bytes: bytes, filename: str, mime_type: str) -> str:
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File too large. Keep uploads under 10MB.")
    try:
        material = await parse_material(client, file_bytes, filename, mime_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not material:
        raise HTTPException(status_code=400, detail="Could not prepare study material from that input.")
    return material


async def create_prepared_session(store: SessionStore, material: str) -> SessionRecord:
    session_id = str(uuid.uuid4())
    record = SessionRecord(session_id=session_id, material=material, material_preview=preview_text(material))
    await store.set(record)
    return record


async def merge_trail_documents(client, documents: list[TrailDocumentRecord]) -> str:
    parts: list[str] = []
    for document in documents:
        body = normalize_text(document.content_text)
        if not body:
            continue
        parts.append(f"Document: {document.filename}\n\n{body}")

    merged_material = normalize_text("\n\n".join(parts))
    if not merged_material:
        raise HTTPException(status_code=400, detail="No readable material exists in this trail yet.")

    return await maybe_summarize_material(client, merged_material)


async def refresh_trail_material(client, trail_store: TrailStore, trail_id: str):
    documents = trail_store.get_trail_documents(trail_id)
    merged_material = await merge_trail_documents(client, documents)
    updated = trail_store.update_trail_material(trail_id, merged_material, preview_text(merged_material))
    if updated is None:
        raise HTTPException(status_code=404, detail="Trail not found.")
    return updated, documents


def humanize_seed_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("-", "_").split("_") if part)


def build_document_tuples_from_material_text(material_text: str, source_name: str) -> list[tuple[str, str, str]]:
    normalized = normalize_text(material_text)
    if not normalized:
        return []
    return [(source_name, "text/plain", normalized)]


def parse_seed_metadata_text(raw_text: str, fallback_name: str) -> tuple[str, str]:
    title = humanize_seed_name(fallback_name)
    description = ""
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        LOGGER.warning("Could not parse trail metadata for %s: %s", fallback_name, exc)
        return title, description

    if isinstance(payload, dict):
        title = str(payload.get("title") or title).strip() or title
        description = str(payload.get("description") or "").strip()
    return title, description


def parse_seed_metadata(seed_dir: Path) -> tuple[str, str]:
    metadata_path = seed_dir / "trail.json"
    if not metadata_path.exists():
        return humanize_seed_name(seed_dir.name), ""

    try:
        return parse_seed_metadata_text(metadata_path.read_text(encoding="utf-8"), seed_dir.name)
    except OSError as exc:
        LOGGER.warning("Could not read trail metadata from %s: %s", metadata_path, exc)
        return humanize_seed_name(seed_dir.name), ""


def iter_seed_document_paths(seed_dir: Path) -> list[Path]:
    ignored_names = {"trail.json", ".ds_store"}
    supported_suffixes = {".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp"}
    return [
        path
        for path in sorted(seed_dir.iterdir())
        if path.is_file()
        and path.name.lower() not in ignored_names
        and path.suffix.lower() in supported_suffixes
    ]


def supported_trail_suffixes() -> set[str]:
    return {".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp"}


def parse_precomputed_payload(
    raw_text: str,
    fallback_name: str,
    default_title: str,
    default_description: str,
) -> tuple[str, str, str, str, list[tuple[str, str, str]]]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid prepared.json for {fallback_name}: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"prepared.json for {fallback_name} must be a JSON object.")

    title = str(payload.get("title") or default_title).strip() or default_title
    description = str(payload.get("description") or default_description).strip()
    material_text = normalize_text(str(payload.get("material_text") or payload.get("text") or ""))
    material_preview = normalize_text(str(payload.get("material_preview") or ""))

    document_entries: list[tuple[str, str, str]] = []
    documents_payload = payload.get("documents")
    if isinstance(documents_payload, list):
        for index, item in enumerate(documents_payload, start=1):
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or f"prepared-doc-{index}.txt").strip()
            mime_type = str(item.get("mime_type") or "text/plain").strip() or "text/plain"
            content_text = normalize_text(str(item.get("content_text") or item.get("text") or ""))
            if content_text:
                document_entries.append((filename, mime_type, content_text))

    if not material_text and document_entries:
        material_text = normalize_text(
            "\n\n".join(f"Document: {filename}\n\n{content_text}" for filename, _, content_text in document_entries)
        )

    if not material_text:
        raise HTTPException(
            status_code=400,
            detail=f"prepared.json for {fallback_name} must include material_text, text, or non-empty documents.",
        )

    if not document_entries:
        document_entries = build_document_tuples_from_material_text(material_text, PRECOMPUTED_JSON_ARTIFACT)

    if not material_preview:
        material_preview = preview_text(material_text)

    return title, description, material_text, material_preview, document_entries


def load_local_precomputed_artifact(
    seed_dir: Path,
    fallback_name: str,
    default_title: str,
    default_description: str,
) -> tuple[str, str, str, str, list[tuple[str, str, str]]] | None:
    prepared_json_path = seed_dir / PRECOMPUTED_JSON_ARTIFACT
    if prepared_json_path.exists():
        try:
            return parse_precomputed_payload(
                prepared_json_path.read_text(encoding="utf-8"),
                fallback_name,
                default_title,
                default_description,
            )
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Could not read {prepared_json_path}: {exc}") from exc

    for artifact_name in PRECOMPUTED_TEXT_ARTIFACTS:
        artifact_path = seed_dir / artifact_name
        if artifact_path.exists():
            try:
                material_text = normalize_text(artifact_path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise HTTPException(status_code=400, detail=f"Could not read {artifact_path}: {exc}") from exc
            if not material_text:
                raise HTTPException(status_code=400, detail=f"{artifact_path} was empty.")
            documents = build_document_tuples_from_material_text(material_text, artifact_name)
            return default_title, default_description, material_text, preview_text(material_text), documents

    return None


async def sync_parsed_documents_into_trail(
    client,
    trail_store: TrailStore,
    trail_id: str,
    parsed_documents: list[tuple[str, str, str]],
):
    if not parsed_documents:
        raise HTTPException(status_code=400, detail="No readable material exists in this trail yet.")

    trail_store.clear_trail_documents(trail_id)
    for filename, mime_type, material in parsed_documents:
        trail_store.add_document(trail_id, filename, mime_type, material)

    updated_trail, documents = await refresh_trail_material(client, trail_store, trail_id)
    return updated_trail, documents


def sync_precomputed_material_into_trail(
    trail_store: TrailStore,
    trail_id: str,
    material_text: str,
    material_preview: str,
    documents: list[tuple[str, str, str]],
):
    trail_store.clear_trail_documents(trail_id)
    for filename, mime_type, content_text in documents:
        trail_store.add_document(trail_id, filename, mime_type, content_text)

    updated_trail = trail_store.update_trail_material(trail_id, material_text, material_preview)
    if updated_trail is None:
        raise HTTPException(status_code=404, detail="Trail not found.")
    return updated_trail, trail_store.get_trail_documents(trail_id)


async def seed_trails_from_directory(client, trail_store: TrailStore, root_dir: Path):
    if not root_dir.exists():
        LOGGER.info("Preset trail seed directory %s does not exist yet; skipping preload.", root_dir)
        return

    seeded_count = 0
    for seed_dir in sorted(path for path in root_dir.iterdir() if path.is_dir()):
        document_paths = iter_seed_document_paths(seed_dir)
        if not document_paths:
            LOGGER.info("Seed trail %s has no supported documents; skipping.", seed_dir.name)
            continue

        title, description = parse_seed_metadata(seed_dir)
        trail = trail_store.upsert_seed_trail(seed_key=seed_dir.name, title=title, description=description)
        precomputed_artifact = load_local_precomputed_artifact(
            seed_dir,
            seed_dir.name,
            trail.title,
            trail.description,
        )
        if precomputed_artifact is not None:
            artifact_title, artifact_description, material_text, material_preview, documents = precomputed_artifact
            trail = trail_store.upsert_seed_trail(
                seed_key=seed_dir.name,
                title=artifact_title,
                description=artifact_description,
            )
            sync_precomputed_material_into_trail(
                trail_store,
                trail.id,
                material_text,
                material_preview,
                documents,
            )
            seeded_count += 1
            continue

        parsed_documents: list[tuple[str, str, str]] = []

        for document_path in document_paths:
            mime_type, _ = mimetypes.guess_type(document_path.name)
            if document_path.suffix.lower() == ".md" and not mime_type:
                mime_type = "text/markdown"
            try:
                material = await prepare_material_bytes(
                    client,
                    document_path.read_bytes(),
                    document_path.name,
                    mime_type or "application/octet-stream",
                )
            except Exception as exc:
                LOGGER.warning("Failed to seed document %s: %s", document_path, exc)
                continue

            parsed_documents.append(
                (
                    document_path.name,
                    mime_type or "application/octet-stream",
                    material,
                )
            )

        if not parsed_documents:
            LOGGER.warning("Seed trail %s produced no readable documents.", seed_dir.name)
            continue

        try:
            await sync_parsed_documents_into_trail(client, trail_store, trail.id, parsed_documents)
        except Exception as exc:
            LOGGER.warning("Failed to refresh seeded trail %s: %s", seed_dir.name, exc)
            continue

        seeded_count += 1

    LOGGER.info("Seeded %d preset trail(s) from %s", seeded_count, root_dir)


def gcs_prefix_for_listing(seed_key: str | None = None) -> str:
    prefix = PRESET_TRAILS_GCS_PREFIX.strip("/")
    if seed_key:
        prefix = f"{prefix}/{seed_key.strip('/')}" if prefix else seed_key.strip("/")
    return f"{prefix}/" if prefix else ""


async def list_gcs_blobs(storage_client, bucket_name: str, prefix: str):
    return await asyncio.to_thread(lambda: list(storage_client.list_blobs(bucket_name, prefix=prefix)))


async def download_gcs_blob_bytes(blob) -> bytes:
    return await asyncio.to_thread(blob.download_as_bytes)


async def load_gcs_precomputed_artifact(
    trail_blobs: list[Any],
    current_seed_key: str,
    default_title: str,
    default_description: str,
) -> tuple[str, str, str, str, list[tuple[str, str, str]]] | None:
    blobs_by_filename = {Path(blob.name).name: blob for blob in trail_blobs}

    if PRECOMPUTED_JSON_ARTIFACT in blobs_by_filename:
        payload_bytes = await download_gcs_blob_bytes(blobs_by_filename[PRECOMPUTED_JSON_ARTIFACT])
        return parse_precomputed_payload(
            payload_bytes.decode("utf-8", errors="ignore"),
            current_seed_key,
            default_title,
            default_description,
        )

    for artifact_name in PRECOMPUTED_TEXT_ARTIFACTS:
        if artifact_name not in blobs_by_filename:
            continue
        material_text = normalize_text(
            (await download_gcs_blob_bytes(blobs_by_filename[artifact_name])).decode("utf-8", errors="ignore")
        )
        if not material_text:
            raise HTTPException(status_code=400, detail=f"{artifact_name} for {current_seed_key} was empty.")
        documents = build_document_tuples_from_material_text(material_text, artifact_name)
        return default_title, default_description, material_text, preview_text(material_text), documents

    return None


async def list_gcs_source_documents(storage_client, bucket_name: str, seed_key: str) -> list[SourceDocument]:
    blobs = await list_gcs_blobs(storage_client, bucket_name, gcs_prefix_for_listing(seed_key))
    documents: list[SourceDocument] = []
    for blob in sorted(blobs, key=lambda item: item.name):
        filename = Path(blob.name).name
        safe_name = safe_document_name(filename)
        if safe_name is None or Path(safe_name).suffix.lower() != ".pdf":
            continue
        documents.append(
            SourceDocument(
                name=safe_name,
                label=pdf_label_from_filename(safe_name),
                mime_type=blob.content_type or "application/pdf",
            )
        )
    return documents


def list_local_source_documents(seed_key: str) -> list[SourceDocument]:
    seed_dir = PRESET_TRAILS_DIR / seed_key
    if not seed_dir.exists():
        return []

    documents: list[SourceDocument] = []
    for path in sorted(seed_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        documents.append(
            SourceDocument(
                name=path.name,
                label=pdf_label_from_filename(path.name),
                mime_type=mimetypes.guess_type(path.name)[0] or "application/pdf",
            )
        )
    return documents


async def get_source_documents_for_trail(app_state, trail: TrailRecord) -> list[SourceDocument]:
    if not trail.seed_key:
        return []
    if app_state.gcs_client is not None and PRESET_TRAILS_GCS_BUCKET:
        return await list_gcs_source_documents(app_state.gcs_client, PRESET_TRAILS_GCS_BUCKET, trail.seed_key)
    return list_local_source_documents(trail.seed_key)


async def download_trail_pdf_bytes(app_state, trail: TrailRecord, filename: str) -> bytes:
    safe_name = safe_document_name(filename)
    if safe_name is None or Path(safe_name).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF source documents can be viewed.")
    if not trail.seed_key:
        raise HTTPException(status_code=404, detail="No source documents are available for this trail.")

    allowed_documents = await get_source_documents_for_trail(app_state, trail)
    if safe_name not in {document.name for document in allowed_documents}:
        raise HTTPException(status_code=404, detail="Source document not found.")

    if app_state.gcs_client is not None and PRESET_TRAILS_GCS_BUCKET:
        blob_name = f"{gcs_prefix_for_listing(trail.seed_key)}{safe_name}"
        bucket = app_state.gcs_client.bucket(PRESET_TRAILS_GCS_BUCKET)
        blob = bucket.blob(blob_name)
        if not await asyncio.to_thread(blob.exists):
            raise HTTPException(status_code=404, detail="Source document not found.")
        return await download_gcs_blob_bytes(blob)

    local_path = PRESET_TRAILS_DIR / trail.seed_key / safe_name
    if not local_path.exists():
        raise HTTPException(status_code=404, detail="Source document not found.")
    return local_path.read_bytes()


async def sync_trails_from_gcs(
    client,
    trail_store: TrailStore,
    storage_client,
    bucket_name: str,
    seed_key: str | None = None,
):
    prefix = gcs_prefix_for_listing(seed_key)
    blobs = await list_gcs_blobs(storage_client, bucket_name, prefix)
    if not blobs:
        LOGGER.info("No preset trail objects found in gs://%s/%s", bucket_name, prefix)
        return []

    grouped_blobs: dict[str, list[Any]] = defaultdict(list)
    supported_suffixes = supported_trail_suffixes()
    for blob in blobs:
        relative_name = blob.name[len(prefix) :] if prefix else blob.name
        if not relative_name or relative_name.endswith("/"):
            continue
        current_seed_key, separator, child_name = relative_name.partition("/")
        if not separator or not child_name:
            continue
        grouped_blobs[current_seed_key].append(blob)

    synced_trails = []
    for current_seed_key in sorted(grouped_blobs.keys()):
        trail_blobs = sorted(grouped_blobs[current_seed_key], key=lambda item: item.name)
        metadata_blob = next((blob for blob in trail_blobs if Path(blob.name).name == "trail.json"), None)

        if metadata_blob is not None:
            metadata_bytes = await download_gcs_blob_bytes(metadata_blob)
            title, description = parse_seed_metadata_text(metadata_bytes.decode("utf-8", errors="ignore"), current_seed_key)
        else:
            title, description = humanize_seed_name(current_seed_key), ""

        trail = trail_store.upsert_seed_trail(seed_key=current_seed_key, title=title, description=description)
        precomputed_artifact = await load_gcs_precomputed_artifact(
            trail_blobs,
            current_seed_key,
            trail.title,
            trail.description,
        )
        if precomputed_artifact is not None:
            artifact_title, artifact_description, material_text, material_preview, documents = precomputed_artifact
            trail = trail_store.upsert_seed_trail(
                seed_key=current_seed_key,
                title=artifact_title,
                description=artifact_description,
            )
            updated_trail, stored_documents = sync_precomputed_material_into_trail(
                trail_store,
                trail.id,
                material_text,
                material_preview,
                documents,
            )
            synced_trails.append(
                {
                    "id": updated_trail.id,
                    "seed_key": current_seed_key,
                    "title": updated_trail.title,
                    "description": updated_trail.description,
                    "document_count": len(stored_documents),
                    "material_preview": updated_trail.material_preview,
                }
            )
            continue

        parsed_documents: list[tuple[str, str, str]] = []

        for blob in trail_blobs:
            filename = Path(blob.name).name
            suffix = Path(filename).suffix.lower()
            if filename == "trail.json" or suffix not in supported_suffixes:
                continue

            mime_type = blob.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            try:
                material = await prepare_material_bytes(
                    client,
                    await download_gcs_blob_bytes(blob),
                    filename,
                    mime_type,
                )
            except Exception as exc:
                LOGGER.warning("Failed to ingest GCS preset document %s: %s", blob.name, exc)
                continue

            parsed_documents.append((filename, mime_type, material))

        if not parsed_documents:
            LOGGER.warning("GCS preset trail %s produced no readable documents.", current_seed_key)
            continue

        updated_trail, documents = await sync_parsed_documents_into_trail(
            client,
            trail_store,
            trail.id,
            parsed_documents,
        )
        synced_trails.append(
            {
                "id": updated_trail.id,
                "seed_key": current_seed_key,
                "title": updated_trail.title,
                "description": updated_trail.description,
                "document_count": len(documents),
                "material_preview": updated_trail.material_preview,
            }
        )

    LOGGER.info("Synced %d preset trail(s) from gs://%s/%s", len(synced_trails), bucket_name, prefix)
    return synced_trails


def clamp_score(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def normalize_scores(raw: dict[str, Any]) -> dict[str, Any]:
    data = {
        "accuracy_score": clamp_score(raw.get("accuracy_score")),
        "completeness_score": clamp_score(raw.get("completeness_score")),
        "clarity_score": clamp_score(raw.get("clarity_score")),
        "depth_score": clamp_score(raw.get("depth_score")),
        "overall_score": clamp_score(raw.get("overall_score")),
        "strengths": [str(item).strip() for item in raw.get("strengths", []) if str(item).strip()][:4],
        "gaps": [str(item).strip() for item in raw.get("gaps", []) if str(item).strip()][:4],
        "misconceptions": [
            str(item).strip() for item in raw.get("misconceptions", []) if str(item).strip()
        ][:4],
        "next_steps": [str(item).strip() for item in raw.get("next_steps", []) if str(item).strip()][:3],
    }
    if not data["strengths"]:
        data["strengths"] = ["Stayed engaged and kept working through the material."]
    if not data["gaps"]:
        data["gaps"] = ["Review the trickiest concept one more time to lock it in."]
    if not data["next_steps"]:
        data["next_steps"] = ["Do one more short practice session on the hardest concept."]
    validated = ScorePayload(**data)
    return validated.model_dump()


async def fallback_score(client, state: RuntimeState) -> dict[str, Any]:
    """Generate structured scores when the live tool call does not arrive."""
    prompt = SCORING_FALLBACK_PROMPT.format(
        mode_name=MODES[state.mode_id]["name"],
        persona_name=PERSONAS[state.persona_id]["name"],
        material=state.material,
        transcript=build_transcript_text(state.transcript),
    )
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SCORE_FUNCTION["parameters"],
        ),
    )
    raw_text = (response.text or "{}").strip()
    try:
        return normalize_scores(json.loads(raw_text))
    except json.JSONDecodeError:
        LOGGER.warning("Fallback scoring returned invalid JSON: %s", raw_text)
        return normalize_scores({})


async def maybe_send_scores(websocket: WebSocket, state: RuntimeState, scores: dict[str, Any]):
    if state.scores is not None:
        return
    state.scores = scores
    await websocket.send_json({"type": "scores", "data": scores})


async def maybe_close_session(websocket: WebSocket, state: RuntimeState):
    if state.session_closed.is_set():
        return
    state.session_closed.set()
    try:
        await websocket.send_json({"type": "session_end"})
    except Exception:  # pragma: no cover - socket already gone
        return


def schedule_close_after_quiet_period(websocket: WebSocket, state: RuntimeState, delay: int = WRAP_UP_WAIT_SECONDS):
    if state.close_task and not state.close_task.done():
        state.close_task.cancel()

    async def _close_later():
        try:
            await asyncio.sleep(delay)
            await maybe_close_session(websocket, state)
        except asyncio.CancelledError:  # pragma: no cover - expected on new audio
            return

    state.close_task = asyncio.create_task(_close_later())


async def append_transcript(
    websocket: WebSocket,
    state: RuntimeState,
    speaker: str,
    text: str,
    finished: bool = True,
    channel: str = "main",
):
    if not text or not text.strip():
        return None

    if channel == "correction":
        last_user_attr = "correction_last_user_text"
        last_agent_attr = "correction_last_agent_text"
        partial_user_attr = "correction_partial_user_text"
        partial_agent_attr = "correction_partial_agent_text"
        transcript_target = state.correction_transcript
    else:
        last_user_attr = "last_user_text"
        last_agent_attr = "last_agent_text"
        partial_user_attr = "partial_user_text"
        partial_agent_attr = "partial_agent_text"
        transcript_target = state.transcript

    if speaker == "user":
        partial_user_text = getattr(state, partial_user_attr)
        combined_raw = f"{partial_user_text}{text}" if partial_user_text else text
        combined_text = combined_raw.strip()
        if combined_text == getattr(state, last_user_attr) and finished:
            return None
        if finished:
            setattr(state, last_user_attr, combined_text)
            setattr(state, partial_user_attr, "")
        else:
            setattr(state, partial_user_attr, combined_raw)
        message_type = "transcript_user"
    else:
        partial_agent_text = getattr(state, partial_agent_attr)
        combined_raw = f"{partial_agent_text}{text}" if partial_agent_text else text
        combined_text = combined_raw.strip()
        if combined_text == getattr(state, last_agent_attr) and finished:
            return None
        if finished:
            setattr(state, last_agent_attr, combined_text)
            setattr(state, partial_agent_attr, "")
        else:
            setattr(state, partial_agent_attr, combined_raw)
        message_type = "transcript_agent"

    if finished:
        transcript_target.append({"speaker": speaker, "text": combined_text})
    await websocket.send_json({"type": message_type, "text": combined_text, "finished": finished, "channel": channel})
    if speaker == "user" and finished and channel == "main":
        LOGGER.info("Scheduling issue detection for finalized user turn: %s", combined_text[:240])
        schedule_issue_detection(websocket, app.state.client, state, combined_text)
    return combined_text if finished else None


async def flush_partial_transcripts(websocket: WebSocket, state: RuntimeState, channel: str = "main"):
    """Finalize any in-progress transcript bubble when Gemini marks a turn complete."""
    finalized_user_texts: list[str] = []
    if channel == "correction":
        partial_user_text = state.correction_partial_user_text
        partial_agent_text = state.correction_partial_agent_text
    else:
        partial_user_text = state.partial_user_text
        partial_agent_text = state.partial_agent_text

    if partial_user_text:
        finalized_text = await append_transcript(websocket, state, "user", partial_user_text, finished=True, channel=channel)
        if finalized_text:
            finalized_user_texts.append(finalized_text)
    if partial_agent_text:
        await append_transcript(websocket, state, "agent", partial_agent_text, finished=True, channel=channel)
    return finalized_user_texts


async def handle_main_tool_call(
    websocket: WebSocket,
    live_session,
    state: RuntimeState,
    tool_call: dict[str, Any],
):
    if tool_call.get("name") != "score_session":
        return

    scores = normalize_scores(tool_call.get("args") or {})
    await maybe_send_scores(websocket, state, scores)
    state.tool_called.set()

    await send_tool_response(
        live_session,
        [
            {
                "id": tool_call.get("id"),
                "name": "score_session",
                "response": {
                    "status": "ok",
                    "message": "Scores received. Give a short spoken wrap-up now.",
                },
            }
        ],
    )
    schedule_close_after_quiet_period(websocket, state)


def normalize_correction_completion(raw: dict[str, Any]) -> dict[str, Any]:
    resolved_claim = normalize_text(str(raw.get("resolved_claim") or ""))
    resolved_summary = normalize_text(str(raw.get("resolved_summary") or ""))
    try:
        confidence = float(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "resolved_claim": resolved_claim,
        "resolved_summary": resolved_summary,
        "confidence": max(0.0, min(confidence, 1.0)),
    }


async def stream_live_events(
    websocket: WebSocket,
    live_session,
    state: RuntimeState,
    channel: str = "main",
    tool_handler=None,
    session_is_active=None,
    on_finalized_user_turn=None,
):
    """Forward Gemini Live output to the frontend as it arrives."""
    LOGGER.info("Receive loop entered for session %s on channel %s", state.session_id, channel)
    response_count = 0
    turn_number = 0

    # session.receive() yields a finite async generator that exhausts after
    # each turn_complete.  Re-enter it for subsequent turns.
    while not state.session_closed.is_set():
        if session_is_active is not None and not session_is_active():
            LOGGER.info(
                "Stopping receive loop for session %s on channel %s because the session is no longer active",
                state.session_id,
                channel,
            )
            break
        async for response in live_session.receive():
            if session_is_active is not None and not session_is_active():
                LOGGER.info(
                    "Dropping receive loop response for session %s on channel %s because the session changed",
                    state.session_id,
                    channel,
                )
                return
            response_count += 1
            # Read SDK response fields directly (no model_dump / dict-scraping)
            result = extract_from_response(response)

            user_texts = result["user_transcripts"]
            agent_texts = result["agent_transcripts"]
            audio_messages = result["audio_chunks"]
            tool_calls = result["tool_calls"]

            if user_texts or agent_texts:
                LOGGER.info(
                    "Live transcription event (channel=%s, turn %d, msg #%d) user=%s agent=%s",
                    channel,
                    turn_number,
                    response_count,
                    user_texts,
                    agent_texts,
                )
            for item in user_texts:
                finalized_text = await append_transcript(
                    websocket,
                    state,
                    "user",
                    item["text"],
                    finished=item.get("finished", True),
                    channel=channel,
                )
                if finalized_text and on_finalized_user_turn is not None:
                    await on_finalized_user_turn(finalized_text)
            for item in agent_texts:
                if channel == "correction" and state.correction_closing_started:
                    agent_text = normalize_text(item["text"])
                    if agent_text and (
                        "handing you back" in agent_text.lower()
                        or issue_signature(agent_text) == issue_signature(CORRECTION_HANDOFF_LINE)
                    ):
                        state.correction_closing_transcript_seen = True
                if channel == "main" and state.interruption_active:
                    continue
                await append_transcript(
                    websocket,
                    state,
                    "agent",
                    item["text"],
                    finished=item.get("finished", True),
                    channel=channel,
                )

            for audio_message in audio_messages:
                if channel == "main" and state.interruption_active:
                    continue
                await websocket.send_json({"type": "audio", "data": audio_message, "channel": channel})

            for tool_call in tool_calls:
                if tool_handler:
                    await tool_handler(websocket, live_session, state, tool_call)

            if result["turn_complete"]:
                finalized_user_texts = await flush_partial_transcripts(websocket, state, channel=channel)
                if on_finalized_user_turn is not None:
                    for finalized_text in finalized_user_texts:
                        await on_finalized_user_turn(finalized_text)
                turn_number += 1
                LOGGER.info(
                    "Turn complete for session %s on channel %s (turn %d, %d responses so far)",
                    state.session_id,
                    channel,
                    turn_number,
                    response_count,
                )

            if result["interrupted"]:
                LOGGER.info("Interrupted flag for session %s on channel %s (turn %d)", state.session_id, channel, turn_number)

            if channel == "main" and state.tool_called.is_set() and not state.session_closed.is_set():
                if agent_texts or audio_messages or tool_calls:
                    schedule_close_after_quiet_period(websocket, state)

        LOGGER.info(
            "Receive generator exhausted for session %s on channel %s after turn %d — re-entering for next turn",
            state.session_id,
            channel,
            turn_number,
        )


async def end_session_with_safety_net(
    websocket: WebSocket,
    client,
    live_session,
    state: RuntimeState,
):
    """Ask the live model to score. Fall back if it does not."""
    await send_text_instruction(
        live_session,
        "The user has ended the session. Call score_session now, then give a short spoken wrap-up.",
    )

    try:
        await asyncio.wait_for(state.tool_called.wait(), timeout=STOP_TOOL_TIMEOUT_SECONDS)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(state.session_closed.wait(), timeout=WRAP_UP_WAIT_SECONDS + 3)
    except asyncio.TimeoutError:
        scores = await fallback_score(client, state)
        await maybe_send_scores(websocket, state, scores)
        try:
            await send_text_instruction(
                live_session,
                WRAP_UP_PROMPT.format(scores_json=json.dumps(scores)),
            )
            await asyncio.sleep(WRAP_UP_WAIT_SECONDS)
        except Exception as exc:  # pragma: no cover - depends on live transport
            LOGGER.warning("Live wrap-up after fallback failed: %s", exc)

    await maybe_close_session(websocket, state)


async def pause_for_interruption(websocket: WebSocket, live_session, state: RuntimeState):
    state.interruption_active = True
    if state.issue_detection_task and not state.issue_detection_task.done():
        state.issue_detection_task.cancel()
    await websocket.send_json({"type": "interruption_started"})
    await send_text_instruction(
        live_session,
        "Pause the current lesson immediately. Do not continue speaking until you receive a resume instruction.",
    )


def register_pending_issue(state: RuntimeState, signature: str = "", claim: str = ""):
    signature = issue_signature(signature)
    claim = normalize_text(claim)
    if signature:
        state.pending_issue_signature = signature
    if claim:
        state.pending_issue_claim = claim
    state.correction_closing_started = False
    state.correction_closing_transcript_seen = False


async def resume_after_interruption(websocket: WebSocket, live_session, state: RuntimeState, summary: str):
    state.interruption_active = False
    if state.pending_issue_signature:
        state.resolved_issue_signatures.add(state.pending_issue_signature)
    if state.pending_issue_claim:
        state.resolved_issue_claims.append(state.pending_issue_claim)
    state.pending_issue_signature = ""
    state.pending_issue_claim = ""
    await websocket.send_json({"type": "interruption_resumed"})
    await send_text_instruction(
        live_session,
        (
            "A correction sidecar just resolved a confusion for the user. "
            f"Here is the clarification to incorporate: {summary.strip()} "
            "Acknowledge the clarification briefly, then continue the lesson from where you left off."
        ),
    )


app = FastAPI(title="TeachBack", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    app.state.client = build_client()
    app.state.gcs_client = build_gcs_client()
    app.state.store = SessionStore()
    app.state.trails = TrailStore(TRAILS_DB_PATH)
    if PRESET_TRAILS_AUTO_SYNC:
        if app.state.gcs_client is not None and PRESET_TRAILS_GCS_BUCKET:
            await sync_trails_from_gcs(
                app.state.client,
                app.state.trails,
                app.state.gcs_client,
                PRESET_TRAILS_GCS_BUCKET,
            )
        elif PRESET_TRAILS_DIR.exists():
            await seed_trails_from_directory(app.state.client, app.state.trails, PRESET_TRAILS_DIR)


@app.post("/api/upload")
async def upload_material(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
):
    material = await prepare_material_input(app.state.client, file=file, text=text)
    record = await create_prepared_session(app.state.store, material)
    return build_session_payload(record)


@app.post("/api/topic")
async def topic_material(payload: TopicPayload):
    try:
        material = await generate_topic_material(app.state.client, payload.topic, payload.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    record = await create_prepared_session(app.state.store, material)
    return build_session_payload(record)


@app.get("/api/personas")
async def get_personas():
    return list_personas()


@app.get("/api/modes")
async def get_modes():
    return list_modes()


@app.post("/api/admin/trails")
async def create_trail(payload: TrailCreatePayload):
    trail = app.state.trails.create_trail(payload.title, payload.description)
    return {
        "id": trail.id,
        "title": trail.title,
        "description": trail.description,
        "material_preview": trail.material_preview,
        "document_count": 0,
    }


@app.post("/api/admin/trails/{trail_id}/documents")
async def add_trail_document(
    trail_id: str,
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    filename: str | None = Form(default=None),
):
    trail = app.state.trails.get_trail(trail_id)
    if trail is None:
        raise HTTPException(status_code=404, detail="Trail not found.")

    material = await prepare_material_input(app.state.client, file=file, text=text)
    inferred_filename = filename or (file.filename if file else None) or f"document-{len(app.state.trails.get_trail_documents(trail_id)) + 1}.txt"
    inferred_mime = file.content_type if file else "text/plain"
    app.state.trails.add_document(trail_id, inferred_filename, inferred_mime or "text/plain", material)
    updated_trail, documents = await refresh_trail_material(app.state.client, app.state.trails, trail_id)

    return {
        "trail": {
            "id": updated_trail.id,
            "title": updated_trail.title,
            "description": updated_trail.description,
            "material_preview": updated_trail.material_preview,
            "material_text": updated_trail.merged_material,
        },
        "documents": [
            {
                "id": document.id,
                "filename": document.filename,
                "mime_type": document.mime_type,
                "created_at": document.created_at,
            }
            for document in documents
        ],
    }


@app.get("/api/admin/trails/{trail_id}")
async def get_trail_detail(trail_id: str):
    trail = app.state.trails.get_trail(trail_id)
    if trail is None:
        raise HTTPException(status_code=404, detail="Trail not found.")
    documents = app.state.trails.get_trail_documents(trail_id)
    return {
        "id": trail.id,
        "title": trail.title,
        "description": trail.description,
        "material_preview": trail.material_preview,
        "material_text": trail.merged_material,
        "documents": [
            {
                "id": document.id,
                "filename": document.filename,
                "mime_type": document.mime_type,
                "created_at": document.created_at,
            }
            for document in documents
        ],
    }


@app.post("/api/admin/trails/sync-gcs")
async def sync_gcs_preset_trails(seed_key: str | None = None):
    if app.state.gcs_client is None or not PRESET_TRAILS_GCS_BUCKET:
        raise HTTPException(
            status_code=400,
            detail="GCS preset trail sync is not configured. Set PRESET_TRAILS_GCS_BUCKET first.",
        )

    trails = await sync_trails_from_gcs(
        app.state.client,
        app.state.trails,
        app.state.gcs_client,
        PRESET_TRAILS_GCS_BUCKET,
        seed_key=seed_key,
    )
    return {
        "bucket": PRESET_TRAILS_GCS_BUCKET,
        "prefix": PRESET_TRAILS_GCS_PREFIX,
        "synced_count": len(trails),
        "trails": trails,
    }


@app.get("/api/preset-trails")
async def get_preset_trails():
    trails = app.state.trails.list_trails()
    ready_trails = [trail for trail in trails if trail.merged_material.strip()]
    return {
        "trails": [
            {
                "id": trail.id,
                "title": trail.title,
                "description": trail.description,
                "material_preview": trail.material_preview,
            }
            for trail in ready_trails
        ]
    }


@app.post("/api/preset-trails/{trail_id}/prepare")
async def prepare_preset_trail(trail_id: str):
    trail = app.state.trails.get_trail(trail_id)
    if trail is None:
        raise HTTPException(status_code=404, detail="Trail not found.")
    if not trail.merged_material.strip():
        raise HTTPException(status_code=400, detail="This trail does not have any prepared material yet.")

    record = await create_prepared_session(app.state.store, trail.merged_material)
    payload = build_session_payload(record)
    payload["learning_goals"] = build_learning_goals_text(trail)
    source_documents = await get_source_documents_for_trail(app.state, trail)
    payload["source_documents"] = [
        {
            "name": document.name,
            "label": document.label,
            "mime_type": document.mime_type,
            "view_url": f"/api/preset-trails/{trail.id}/documents/{quote(document.name)}",
        }
        for document in source_documents
    ]
    return payload


@app.get("/api/preset-trails/{trail_id}/documents/{filename}")
async def view_preset_trail_document(trail_id: str, filename: str):
    trail = app.state.trails.get_trail(trail_id)
    if trail is None:
        raise HTTPException(status_code=404, detail="Trail not found.")

    pdf_bytes = await download_trail_pdf_bytes(app.state, trail, filename)
    safe_name = Path(filename).name
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.websocket("/ws/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: str):
    await websocket.accept()
    LOGGER.info("WebSocket accepted for session %s", session_id)

    record = await app.state.store.get(session_id)
    if record is None:
        await websocket.send_json({"type": "error", "message": "Session expired or not found."})
        await websocket.close()
        return

    main_receive_task = None
    correction_receive_task = None
    main_live_session_cm = None
    correction_live_session_cm = None
    main_live_session = None
    correction_live_session = None
    state: RuntimeState | None = None

    async def cancel_task(task: asyncio.Task | None):
        if task is None:
            return
        task.cancel()
        current_task = asyncio.current_task()
        if task is current_task:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    try:
        LOGGER.info("Waiting for start message for session %s", session_id)
        start_message = await websocket.receive_json()
        LOGGER.info("Received start payload for session %s: %s", session_id, start_message)
        if start_message.get("type") != "start":
            await websocket.send_json({"type": "error", "message": "Expected a start message."})
            return

        mode_id = start_message.get("mode_id")
        persona_id = start_message.get("persona_id")
        if mode_id not in MODES or persona_id not in PERSONAS:
            await websocket.send_json({"type": "error", "message": "Invalid mode or persona."})
            return

        state = RuntimeState(
            session_id=session_id,
            mode_id=mode_id,
            persona_id=persona_id,
            material=record.material,
        )
        audio_debug: dict[str, dict[str, Any]] = {
            "main": {"buffer": bytearray(), "sample_rate": 0, "chunk_count": 0},
            "correction": {"buffer": bytearray(), "sample_rate": 0, "chunk_count": 0},
        }

        def log_receive_task_failure(label: str):
            def _callback(task: asyncio.Task):
                if task.cancelled():
                    return
                exc = task.exception()
                if exc:
                    LOGGER.exception("%s receive task failed for session %s", label, session_id, exc_info=exc)

            return _callback

        async def close_main_live_session():
            nonlocal main_receive_task, main_live_session_cm, main_live_session
            await cancel_task(main_receive_task)
            main_receive_task = None
            if main_live_session_cm is not None:
                await main_live_session_cm.__aexit__(None, None, None)
            main_live_session_cm = None
            main_live_session = None

        async def close_correction_live_session():
            nonlocal correction_receive_task, correction_live_session_cm, correction_live_session
            if state.correction_verification_task and not state.correction_verification_task.done():
                verification_task = state.correction_verification_task
                state.correction_verification_task.cancel()
                if verification_task is not asyncio.current_task():
                    with contextlib.suppress(asyncio.CancelledError):
                        await verification_task
            state.correction_verification_task = None
            if state.correction_timeout_task and not state.correction_timeout_task.done():
                timeout_task = state.correction_timeout_task
                state.correction_timeout_task.cancel()
                if timeout_task is not asyncio.current_task():
                    with contextlib.suppress(asyncio.CancelledError):
                        await timeout_task
            state.correction_timeout_task = None
            await cancel_task(correction_receive_task)
            correction_receive_task = None
            if correction_live_session_cm is not None:
                await correction_live_session_cm.__aexit__(None, None, None)
            correction_live_session_cm = None
            correction_live_session = None
            state.correction_partial_user_text = ""
            state.correction_partial_agent_text = ""
            state.correction_last_user_text = ""
            state.correction_last_agent_text = ""
            state.pending_issue_expected_correction = ""
            state.correction_closing_started = False
            state.correction_closing_transcript_seen = False

        async def open_main_live_session(continuation_context: str = ""):
            nonlocal main_receive_task, main_live_session_cm, main_live_session
            await close_main_live_session()
            LOGGER.info(
                "Opening Gemini Live main session for %s with mode=%s persona=%s",
                session_id,
                mode_id,
                persona_id,
            )
            main_live_session_cm = create_live_session(
                app.state.client,
                mode_id,
                persona_id,
                record.material,
                continuation_context=continuation_context,
            )
            main_live_session = await main_live_session_cm.__aenter__()
            try:
                sig = inspect.signature(main_live_session.send_realtime_input)
                LOGGER.info("main send_realtime_input params: %s", list(sig.parameters.keys()))
            except Exception:
                pass
            main_receive_task = asyncio.create_task(
                stream_live_events(
                    websocket,
                    main_live_session,
                    state,
                    channel="main",
                    tool_handler=handle_main_tool_call,
                    session_is_active=lambda: main_live_session is not None,
                    on_finalized_user_turn=None,
                )
            )
            main_receive_task.add_done_callback(log_receive_task_failure("Main"))
            state.orchestrator_state = "main_active"

        async def reopen_main_live_session_with_continuation(resolved_claim: str, resolved_summary: str):
            continuation_context = normalize_text(
                f"""
Recent transcript excerpt:
{recent_transcript_excerpt(state.transcript, limit=8)}

Resolved correction:
Claim: {resolved_claim}
Summary: {resolved_summary}

Resume the lesson naturally. Acknowledge the correction briefly if needed, then continue from the existing lesson context.
"""
            )
            await open_main_live_session(continuation_context=continuation_context)

        async def inject_correction_into_main(resolved_claim: str, resolved_summary: str):
            prompt = normalize_text(
                f"""
A correction agent just resolved a confusion for the user.

Resolved claim:
{resolved_claim}

Correction to incorporate:
{resolved_summary}

Acknowledge the correction briefly, then continue the lesson from where you left off.
"""
            )
            try:
                if main_live_session is None:
                    raise RuntimeError("Main live session is unavailable.")
                await send_text_instruction(main_live_session, prompt)
            except Exception as exc:
                LOGGER.warning("Main session correction injection failed, recreating main session: %s", exc)
                await reopen_main_live_session_with_continuation(resolved_claim, resolved_summary)

        async def complete_correction_and_resume(resolved_claim: str, resolved_summary: str, confidence: float):
            resolved_claim = normalize_text(resolved_claim or state.pending_issue_claim)
            resolved_summary = normalize_text(resolved_summary)
            if not resolved_summary:
                return
            if state.correction_completion_task and state.correction_completion_task is not asyncio.current_task():
                if state.correction_completion_task.done():
                    state.correction_completion_task = None
            if state.correction_closing_started:
                return

            if state.pending_issue_signature:
                state.resolved_issue_signatures.add(state.pending_issue_signature)
            if resolved_claim:
                state.resolved_issue_claims.append(resolved_claim)
            state.pending_issue_signature = ""
            state.pending_issue_claim = ""
            state.pending_issue_expected_correction = ""
            state.orchestrator_state = "correction_closing"
            state.correction_closing_started = True
            if correction_live_session is not None:
                with contextlib.suppress(Exception):
                    await send_text_instruction(
                        correction_live_session,
                        f'Say exactly this one sentence, then stop: "{CORRECTION_HANDOFF_LINE}"',
                    )
                deadline = asyncio.get_running_loop().time() + 1.0
                while asyncio.get_running_loop().time() < deadline:
                    if state.correction_closing_transcript_seen:
                        break
                    await asyncio.sleep(0.08)
            await websocket.send_json(
                {
                    "type": "correction_complete",
                    "data": {
                        "resolved_claim": resolved_claim,
                        "resolved_summary": resolved_summary,
                        "confidence": confidence,
                    },
                }
            )
            await close_correction_live_session()
            state.interruption_active = False
            state.orchestrator_state = "main_resuming"
            await inject_correction_into_main(resolved_claim, resolved_summary)
            state.orchestrator_state = "main_active"
            await websocket.send_json(
                {
                    "type": "interruption_resumed",
                    "data": {
                        "resolved_claim": resolved_claim,
                        "resolved_summary": resolved_summary,
                        "reason": "completed",
                    },
                }
            )

        async def schedule_correction_verification(latest_user_text: str):
            latest_user_text = normalize_text(latest_user_text)
            if not latest_user_text or not state.interruption_active or state.correction_closing_started:
                return
            if not state.pending_issue_claim or not state.pending_issue_expected_correction:
                return
            if state.correction_verification_task and not state.correction_verification_task.done():
                state.correction_verification_task.cancel()

            async def _run():
                try:
                    state.orchestrator_state = "correction_verifying"
                    verification = await verify_correction_turn(
                        app.state.client,
                        state.pending_issue_claim,
                        state.pending_issue_expected_correction,
                        latest_user_text,
                        recent_transcript_excerpt(state.correction_transcript, limit=4),
                    )
                except asyncio.CancelledError:
                    return
                    if not verification or state.correction_closing_started or not state.interruption_active:
                        if state.interruption_active and not state.correction_closing_started:
                            state.orchestrator_state = "correction_active"
                        return
                if state.correction_completion_task and not state.correction_completion_task.done():
                    return
                state.correction_completion_task = asyncio.create_task(
                    complete_correction_and_resume(
                        state.pending_issue_claim,
                        verification["resolved_summary"],
                        verification["confidence"],
                    )
                )

            state.correction_verification_task = asyncio.create_task(_run())

        async def handle_correction_tool_call(
            websocket: WebSocket,
            live_session,
            state: RuntimeState,
            tool_call: dict[str, Any],
        ):
            if tool_call.get("name") != "complete_correction":
                return

            completion = normalize_correction_completion(tool_call.get("args") or {})
            if not completion["resolved_summary"]:
                return

            await send_tool_response(
                live_session,
                [
                    {
                        "id": tool_call.get("id"),
                        "name": "complete_correction",
                        "response": {
                            "status": "ok",
                            "message": (
                                "Correction accepted. In one short sentence, tell the learner they now have the "
                                "right idea and that you are handing them back to the main tutor."
                            ),
                        },
                    }
                ],
            )
            if state.correction_closing_started:
                return
            if state.correction_completion_task and not state.correction_completion_task.done():
                return
            state.correction_completion_task = asyncio.create_task(
                complete_correction_and_resume(
                    completion["resolved_claim"],
                    completion["resolved_summary"],
                    completion["confidence"],
                )
            )

        async def start_correction_session(issue_payload: dict[str, Any]):
            nonlocal correction_receive_task, correction_live_session_cm, correction_live_session
            if correction_live_session is not None:
                return

            issue_claim = normalize_text(str(issue_payload.get("claim") or "Potential misconception"))
            issue_prompt = normalize_text(
                str(issue_payload.get("prompt") or "Restate the corrected idea clearly in your own words.")
            )
            suggested_correction = normalize_text(str(issue_payload.get("suggestedCorrection") or issue_claim))
            issue_signature_value = normalize_text(
                str(issue_payload.get("issue_signature") or issue_claim)
            )
            register_pending_issue(state, issue_signature_value, issue_claim)
            state.pending_issue_expected_correction = suggested_correction
            state.interruption_active = True
            state.orchestrator_state = "correction_connecting"
            if state.issue_detection_task and not state.issue_detection_task.done():
                state.issue_detection_task.cancel()

            await websocket.send_json({"type": "interruption_started", "data": {"issue": issue_payload}})
            if main_live_session is not None:
                with contextlib.suppress(Exception):
                    await send_text_instruction(
                        main_live_session,
                        "Pause the current lesson immediately. The correction agent is taking over briefly.",
                    )
            try:
                correction_live_session_cm = create_correction_session(
                    app.state.client,
                    record.material,
                    issue_claim,
                    issue_prompt,
                    suggested_correction,
                )
                correction_live_session = await correction_live_session_cm.__aenter__()
                correction_receive_task = asyncio.create_task(
                    stream_live_events(
                        websocket,
                        correction_live_session,
                    state,
                    channel="correction",
                    tool_handler=handle_correction_tool_call,
                    session_is_active=lambda: correction_live_session is not None,
                    on_finalized_user_turn=schedule_correction_verification,
                )
            )
                correction_receive_task.add_done_callback(log_receive_task_failure("Correction"))
                async def correction_timeout_watch():
                    try:
                        await asyncio.sleep(CORRECTION_TIMEOUT_SECONDS)
                    except asyncio.CancelledError:
                        return
                    if (
                        state.session_closed.is_set()
                        or not state.interruption_active
                        or correction_live_session is None
                    ):
                        return
                    LOGGER.info("Correction session timed out for session %s", session_id)
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "The correction agent took too long. Resuming the main lesson automatically.",
                        }
                    )
                    await force_resume_main(reason="timeout")

                state.correction_timeout_task = asyncio.create_task(correction_timeout_watch())
                state.orchestrator_state = "correction_active"
                await websocket.send_json({"type": "correction_ready", "data": {"issue": issue_payload}})
                await send_text_instruction(
                    correction_live_session,
                    "Introduce yourself in one short sentence, explain the misconception briefly, and ask the learner to correct it.",
                )
            except Exception as exc:
                LOGGER.warning("Correction session failed to start: %s", exc)
                await close_correction_live_session()
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "The correction agent could not start cleanly. Use Force resume to continue the main lesson.",
                    }
                )

        async def force_resume_main(reason: str = "manual"):
            state.interruption_active = False
            state.pending_issue_signature = ""
            state.pending_issue_claim = ""
            state.pending_issue_expected_correction = ""
            state.correction_closing_started = False
            state.correction_closing_transcript_seen = False
            await close_correction_live_session()
            state.orchestrator_state = "main_active"
            await websocket.send_json(
                {
                    "type": "interruption_resumed",
                    "data": {
                        "resolved_claim": "",
                        "resolved_summary": "",
                        "reason": reason,
                    },
                }
            )

        await open_main_live_session()
        await websocket.send_json({"type": "ready"})
        LOGGER.info("Sent ready message to client for session %s", session_id)

        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            LOGGER.info("Received websocket message for session %s: %s", session_id, message_type)

            if message_type == "audio":
                try:
                    pcm_bytes = base64.b64decode(message.get("data", ""))
                except Exception:
                    await websocket.send_json({"type": "error", "message": "Invalid audio payload."})
                    continue

                channel = str(message.get("channel") or ("correction" if state.interruption_active else "main"))
                target_session = correction_live_session if channel == "correction" else main_live_session
                if target_session is None:
                    await websocket.send_json({"type": "error", "message": f"{channel.title()} agent is not available."})
                    continue

                audio_state = audio_debug[channel]
                client_rate = message.get("sampleRate")
                if client_rate:
                    audio_state["sample_rate"] = int(client_rate)
                elif audio_state["sample_rate"] == 0:
                    audio_state["sample_rate"] = 48000 if len(pcm_bytes) >= 8000 else 16000
                audio_state["chunk_count"] += 1
                audio_state["buffer"].extend(pcm_bytes)
                await send_audio_chunk(target_session, pcm_bytes, audio_state["sample_rate"])

            elif message_type == "audio_stream_end":
                channel = str(message.get("channel") or ("correction" if state.interruption_active else "main"))
                target_session = correction_live_session if channel == "correction" else main_live_session
                if target_session is not None:
                    with contextlib.suppress(Exception):
                        await target_session.send_realtime_input(audio_stream_end=True)
                audio_debug[channel]["buffer"] = bytearray()
                audio_debug[channel]["chunk_count"] = 0

            elif message_type == "stop":
                await close_correction_live_session()
                if main_live_session is not None:
                    await main_live_session.send_realtime_input(audio_stream_end=True)
                    await end_session_with_safety_net(websocket, app.state.client, main_live_session, state)
                break

            elif message_type == "start_correction":
                issue_payload = message.get("issue") or {}
                await start_correction_session(issue_payload)

            elif message_type == "cancel_correction":
                await force_resume_main(reason="manual")

    except WebSocketDisconnect:
        LOGGER.info("Client disconnected from session %s", session_id)
    except Exception as exc:
        LOGGER.exception("Session error")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:  # pragma: no cover - socket may already be closed
            pass
    finally:
        await cancel_task(correction_receive_task)
        await cancel_task(main_receive_task)
        if correction_live_session_cm is not None:
            await correction_live_session_cm.__aexit__(None, None, None)
        if main_live_session_cm is not None:
            await main_live_session_cm.__aexit__(None, None, None)
        if state and state.issue_detection_task:
            state.issue_detection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.issue_detection_task
        if state and state.correction_completion_task:
            state.correction_completion_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.correction_completion_task
        if state and state.correction_verification_task:
            state.correction_verification_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.correction_verification_task
        if state and state.correction_timeout_task:
            state.correction_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.correction_timeout_task
        if state and state.close_task:
            state.close_task.cancel()
        if state and state.session_closed.is_set():
            await app.state.store.delete(session_id)


if STATIC_DIR.exists():
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/", include_in_schema=False)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse({"message": "TeachBack API is running."})


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    if full_path.startswith(("api/", "ws/", "health")):
        raise HTTPException(status_code=404, detail="Not found")

    requested = STATIC_DIR / full_path
    if requested.exists() and requested.is_file():
        return FileResponse(requested)

    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Not found")
