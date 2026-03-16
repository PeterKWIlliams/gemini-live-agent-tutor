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
    transcript: list[dict[str, str]] = field(default_factory=list)
    tool_called: asyncio.Event = field(default_factory=asyncio.Event)
    session_closed: asyncio.Event = field(default_factory=asyncio.Event)
    scores: dict[str, Any] | None = None
    last_user_text: str = ""
    last_agent_text: str = ""
    partial_user_text: str = ""
    partial_agent_text: str = ""
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


def build_transcript_text(messages: list[dict[str, str]]) -> str:
    return "\n".join(f"{item['speaker']}: {item['text']}" for item in messages if item["text"].strip())


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
):
    if not text or not text.strip():
        return

    if speaker == "user":
        combined_raw = f"{state.partial_user_text}{text}" if state.partial_user_text else text
        combined_text = combined_raw.strip()
        if combined_text == state.last_user_text and finished:
            return
        if finished:
            state.last_user_text = combined_text
            state.partial_user_text = ""
        else:
            state.partial_user_text = combined_raw
        message_type = "transcript_user"
    else:
        combined_raw = f"{state.partial_agent_text}{text}" if state.partial_agent_text else text
        combined_text = combined_raw.strip()
        if combined_text == state.last_agent_text and finished:
            return
        if finished:
            state.last_agent_text = combined_text
            state.partial_agent_text = ""
        else:
            state.partial_agent_text = combined_raw
        message_type = "transcript_agent"

    if finished:
        state.transcript.append({"speaker": speaker, "text": combined_text})
    await websocket.send_json({"type": message_type, "text": combined_text, "finished": finished})


async def flush_partial_transcripts(websocket: WebSocket, state: RuntimeState):
    """Finalize any in-progress transcript bubble when Gemini marks a turn complete."""
    if state.partial_user_text:
        await append_transcript(websocket, state, "user", state.partial_user_text, finished=True)
    if state.partial_agent_text:
        await append_transcript(websocket, state, "agent", state.partial_agent_text, finished=True)


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
    LOGGER.info("Receive loop entered for session %s", state.session_id)
    response_count = 0
    turn_number = 0

    # session.receive() yields a finite async generator that exhausts after
    # each turn_complete.  Re-enter it for subsequent turns.
    while not state.session_closed.is_set():
        async for response in live_session.receive():
            response_count += 1
            # Read SDK response fields directly (no model_dump / dict-scraping)
            result = extract_from_response(response)

            user_texts = result["user_transcripts"]
            agent_texts = result["agent_transcripts"]
            audio_messages = result["audio_chunks"]
            tool_calls = result["tool_calls"]

            if user_texts or agent_texts:
                LOGGER.info(
                    "Live transcription event (turn %d, msg #%d) user=%s agent=%s",
                    turn_number,
                    response_count,
                    user_texts,
                    agent_texts,
                )
            for item in user_texts:
                await append_transcript(
                    websocket,
                    state,
                    "user",
                    item["text"],
                    finished=item.get("finished", True),
                )
            for item in agent_texts:
                await append_transcript(
                    websocket,
                    state,
                    "agent",
                    item["text"],
                    finished=item.get("finished", True),
                )

            for audio_message in audio_messages:
                await websocket.send_json({"type": "audio", "data": audio_message})

            for tool_call in tool_calls:
                await handle_tool_call(websocket, live_session, state, tool_call)

            if result["turn_complete"]:
                await flush_partial_transcripts(websocket, state)
                turn_number += 1
                LOGGER.info(
                    "Turn complete for session %s (turn %d, %d responses so far)",
                    state.session_id,
                    turn_number,
                    response_count,
                )

            if result["interrupted"]:
                LOGGER.info("Interrupted flag for session %s (turn %d)", state.session_id, turn_number)

            if state.tool_called.is_set() and not state.session_closed.is_set():
                if agent_texts or audio_messages or tool_calls:
                    schedule_close_after_quiet_period(websocket, state)

        LOGGER.info(
            "Receive generator exhausted for session %s after turn %d — re-entering for next turn",
            state.session_id,
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

    receive_task = None
    state: RuntimeState | None = None

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

        LOGGER.info(
            "Opening Gemini Live session for session %s with mode=%s persona=%s",
            session_id,
            mode_id,
            persona_id,
        )
        async with create_live_session(app.state.client, mode_id, persona_id, record.material) as live_session:
            LOGGER.info("Gemini Live session connected for session %s", session_id)

            # One-time: log the SDK method signature so we can verify param names
            try:
                sig = inspect.signature(live_session.send_realtime_input)
                LOGGER.info("send_realtime_input params: %s", list(sig.parameters.keys()))
            except Exception:
                pass

            receive_task = asyncio.create_task(stream_live_events(websocket, live_session, state))
            LOGGER.info("Started receive task for session %s", session_id)

            def _log_receive_task_failure(task: asyncio.Task):
                if task.cancelled():
                    return
                exc = task.exception()
                if exc:
                    LOGGER.exception("Receive task failed for session %s", session_id, exc_info=exc)

            receive_task.add_done_callback(_log_receive_task_failure)

            await websocket.send_json({"type": "ready"})
            LOGGER.info("Sent ready message to client for session %s", session_id)

            # Debug: accumulate audio to save as WAV for inspection
            audio_debug_buffer = bytearray()
            audio_chunk_count = 0
            audio_sample_rate = 0  # detected from first chunk
            audio_turn_number = 0  # incremented on each audio_stream_end

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
                    # Detect sample rate: prefer client-supplied field,
                    # fall back to inferring from chunk size (4096-sample
                    # ScriptProcessor buffer → 8192 bytes at native rate).
                    client_rate = message.get("sampleRate")
                    if client_rate:
                        audio_sample_rate = int(client_rate)
                    elif audio_sample_rate == 0:
                        # 8192 bytes = 4096 samples → 48 kHz native
                        # 2730 bytes = 1365 samples → old 16 kHz resample
                        audio_sample_rate = 48000 if len(pcm_bytes) >= 8000 else 16000
                    audio_chunk_count += 1
                    audio_debug_buffer.extend(pcm_bytes)
                    if audio_chunk_count == 1:
                        LOGGER.info(
                            "Turn %d first audio chunk: %d bytes, rate=%d (from_client=%s)",
                            audio_turn_number, len(pcm_bytes), audio_sample_rate, client_rate,
                        )
                    await send_audio_chunk(live_session, pcm_bytes, audio_sample_rate)

                elif message_type == "audio_stream_end":
                    LOGGER.info(
                        "Ignoring intermediate audio_stream_end for session %s turn %d (%d chunks, %d bytes total)",
                        session_id, audio_turn_number, audio_chunk_count, len(audio_debug_buffer),
                    )
                    audio_turn_number += 1
                    # Save first utterance as WAV for diagnosis
                    if audio_debug_buffer:
                        wav_path = f"/tmp/teachback_debug_{session_id[:8]}.wav"
                        try:
                            with wave.open(wav_path, "wb") as wf:
                                wf.setnchannels(1)
                                wf.setsampwidth(2)
                                wf.setframerate(audio_sample_rate or 48000)
                                wf.writeframes(bytes(audio_debug_buffer))
                            LOGGER.info("Saved debug audio to %s (rate=%d)", wav_path, audio_sample_rate)
                        except Exception as wav_exc:
                            LOGGER.warning("Failed to save debug WAV: %s", wav_exc)
                    audio_debug_buffer = bytearray()
                    audio_chunk_count = 0

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
            with contextlib.suppress(asyncio.CancelledError):
                await receive_task
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
