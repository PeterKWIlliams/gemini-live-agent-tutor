"""FastAPI entrypoint for TeachBack."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from server.gemini_session import (
    create_live_session,
    extract_audio_messages,
    extract_tool_calls,
    extract_transcripts,
    response_to_dict,
    send_audio_chunk,
    send_text_instruction,
    send_tool_response,
)
from server.material_parser import generate_topic_material, maybe_summarize_material, normalize_text, parse_material
from server.modes import MODES, list_modes
from server.personas import PERSONAS, list_personas
from server.scoring import SCORING_FALLBACK_PROMPT, SCORE_FUNCTION, WRAP_UP_PROMPT

load_dotenv()

LOGGER = logging.getLogger("teachback")
logging.basicConfig(level=logging.INFO)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SESSION_TTL = timedelta(minutes=15)
STOP_TOOL_TIMEOUT_SECONDS = 8
WRAP_UP_WAIT_SECONDS = 4
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class TopicPayload(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=4000)


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
    transcript: list[dict[str, str]] = field(default_factory=list)
    tool_called: asyncio.Event = field(default_factory=asyncio.Event)
    session_closed: asyncio.Event = field(default_factory=asyncio.Event)
    scores: dict[str, Any] | None = None
    last_user_text: str = ""
    last_agent_text: str = ""
    close_task: asyncio.Task | None = None


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


def preview_text(material: str) -> str:
    compact = material.replace("\n", " ").strip()
    return compact[:200] + ("..." if len(compact) > 200 else "")


def build_transcript_text(messages: list[dict[str, str]]) -> str:
    return "\n".join(f"{item['speaker']}: {item['text']}" for item in messages if item["text"].strip())


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
):
    text = text.strip()
    if not text:
        return

    if speaker == "user":
        if text == state.last_user_text:
            return
        state.last_user_text = text
        message_type = "transcript_user"
    else:
        if text == state.last_agent_text:
            return
        state.last_agent_text = text
        message_type = "transcript_agent"

    state.transcript.append({"speaker": speaker, "text": text})
    await websocket.send_json({"type": message_type, "text": text})


async def handle_tool_call(
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


async def stream_live_events(
    websocket: WebSocket,
    live_session,
    state: RuntimeState,
):
    """Forward Gemini Live output to the frontend as it arrives."""
    async for response in live_session.receive():
        payload = response_to_dict(response)

        user_texts, agent_texts = extract_transcripts(payload)
        for text in user_texts:
            await append_transcript(websocket, state, "user", text)
        for text in agent_texts:
            await append_transcript(websocket, state, "agent", text)

        audio_messages = extract_audio_messages(payload)
        for audio_message in audio_messages:
            await websocket.send_json({"type": "audio", "data": audio_message})

        tool_calls = extract_tool_calls(payload)
        for tool_call in tool_calls:
            await handle_tool_call(websocket, live_session, state, tool_call)

        if state.tool_called.is_set() and not state.session_closed.is_set():
            if agent_texts or audio_messages or tool_calls:
                schedule_close_after_quiet_period(websocket, state)


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
    app.state.store = SessionStore()


@app.post("/api/upload")
async def upload_material(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
):
    if file is None and not (text or "").strip():
        raise HTTPException(status_code=400, detail="Provide a file or pasted text.")

    client = app.state.client

    if (text or "").strip():
        material = await maybe_summarize_material(client, normalize_text(text or ""))
    else:
        file_bytes = await file.read()
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="File too large. Keep uploads under 10MB.")
        try:
            material = await parse_material(client, file_bytes, file.filename or "upload", file.content_type or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not material:
        raise HTTPException(status_code=400, detail="Could not prepare study material from that input.")

    session_id = str(uuid.uuid4())
    record = SessionRecord(session_id=session_id, material=material, material_preview=preview_text(material))
    await app.state.store.set(record)

    return {"session_id": session_id, "material_preview": record.material_preview}


@app.post("/api/topic")
async def topic_material(payload: TopicPayload):
    try:
        material = await generate_topic_material(app.state.client, payload.topic, payload.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session_id = str(uuid.uuid4())
    record = SessionRecord(session_id=session_id, material=material, material_preview=preview_text(material))
    await app.state.store.set(record)
    return {"session_id": session_id, "material_preview": record.material_preview}


@app.get("/api/personas")
async def get_personas():
    return list_personas()


@app.get("/api/modes")
async def get_modes():
    return list_modes()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.websocket("/ws/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: str):
    await websocket.accept()

    record = await app.state.store.get(session_id)
    if record is None:
        await websocket.send_json({"type": "error", "message": "Session expired or not found."})
        await websocket.close()
        return

    receive_task = None
    state: RuntimeState | None = None

    try:
        start_message = await websocket.receive_json()
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

        async with create_live_session(app.state.client, mode_id, persona_id, record.material) as live_session:
            await websocket.send_json({"type": "ready"})

            kickoff = (
                f"You are starting a {MODES[mode_id]['name']} session as {PERSONAS[persona_id]['name']}. "
                "Greet the user warmly, explain the vibe of the session in one short sentence, and begin."
            )
            await send_text_instruction(live_session, kickoff)
            receive_task = asyncio.create_task(stream_live_events(websocket, live_session, state))

            while True:
                message = await websocket.receive_json()
                message_type = message.get("type")

                if message_type == "audio":
                    try:
                        pcm_bytes = base64.b64decode(message.get("data", ""))
                    except Exception:
                        await websocket.send_json({"type": "error", "message": "Invalid audio payload."})
                        continue
                    await send_audio_chunk(live_session, pcm_bytes)

                elif message_type == "stop":
                    await live_session.send_realtime_input(audio_stream_end=True)
                    await end_session_with_safety_net(websocket, app.state.client, live_session, state)
                    break

    except WebSocketDisconnect:
        LOGGER.info("Client disconnected from session %s", session_id)
    except Exception as exc:
        LOGGER.exception("Session error")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:  # pragma: no cover - socket may already be closed
            pass
    finally:
        if receive_task:
            receive_task.cancel()
            with contextlib.suppress(Exception):
                await receive_task
        if state and state.close_task:
            state.close_task.cancel()
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
