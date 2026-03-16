"""SQLite-backed storage for preset document trails."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TrailRecord:
    id: str
    title: str
    description: str
    seed_key: str | None
    merged_material: str
    material_preview: str
    created_at: str
    updated_at: str


@dataclass
class TrailDocumentRecord:
    id: str
    trail_id: str
    filename: str
    mime_type: str
    content_text: str
    created_at: str


class TrailStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self):
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trails (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    seed_key TEXT UNIQUE,
                    merged_material TEXT NOT NULL DEFAULT '',
                    material_preview TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trail_documents (
                    id TEXT PRIMARY KEY,
                    trail_id TEXT NOT NULL REFERENCES trails(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    content_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(trails)").fetchall()
            }
            if "seed_key" not in columns:
                connection.execute("ALTER TABLE trails ADD COLUMN seed_key TEXT")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trails_seed_key
                ON trails(seed_key)
                WHERE seed_key IS NOT NULL
                """
            )
            connection.commit()

    def create_trail(self, title: str, description: str = "", seed_key: str | None = None) -> TrailRecord:
        record = TrailRecord(
            id=str(uuid.uuid4()),
            title=title.strip(),
            description=description.strip(),
            seed_key=seed_key.strip() if seed_key else None,
            merged_material="",
            material_preview="",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trails (id, title, description, seed_key, merged_material, material_preview, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.title,
                    record.description,
                    record.seed_key,
                    record.merged_material,
                    record.material_preview,
                    record.created_at,
                    record.updated_at,
                ),
            )
            connection.commit()
        return record

    def upsert_seed_trail(self, seed_key: str, title: str, description: str = "") -> TrailRecord:
        seed_key = seed_key.strip()
        title = title.strip()
        description = description.strip()
        existing = self.get_trail_by_seed_key(seed_key)
        if existing is None:
            return self.create_trail(title=title, description=description, seed_key=seed_key)

        updated_at = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE trails
                SET title = ?, description = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, description, updated_at, existing.id),
            )
            connection.commit()
        return self.get_trail(existing.id)  # type: ignore[return-value]

    def add_document(self, trail_id: str, filename: str, mime_type: str, content_text: str) -> TrailDocumentRecord:
        record = TrailDocumentRecord(
            id=str(uuid.uuid4()),
            trail_id=trail_id,
            filename=filename.strip(),
            mime_type=(mime_type or "text/plain").strip(),
            content_text=content_text,
            created_at=utc_now(),
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trail_documents (id, trail_id, filename, mime_type, content_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.trail_id,
                    record.filename,
                    record.mime_type,
                    record.content_text,
                    record.created_at,
                ),
            )
            connection.commit()
        return record

    def update_trail_material(self, trail_id: str, merged_material: str, material_preview: str) -> TrailRecord | None:
        updated_at = utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE trails
                SET merged_material = ?, material_preview = ?, updated_at = ?
                WHERE id = ?
                """,
                (merged_material, material_preview, updated_at, trail_id),
            )
            if cursor.rowcount == 0:
                return None
            connection.commit()
        return self.get_trail(trail_id)

    def list_trails(self) -> list[TrailRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, description, seed_key, merged_material, material_preview, created_at, updated_at
                FROM trails
                ORDER BY updated_at DESC, created_at DESC
                """
            ).fetchall()
        return [TrailRecord(**dict(row)) for row in rows]

    def get_trail(self, trail_id: str) -> TrailRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, title, description, seed_key, merged_material, material_preview, created_at, updated_at
                FROM trails
                WHERE id = ?
                """,
                (trail_id,),
            ).fetchone()
        return TrailRecord(**dict(row)) if row else None

    def get_trail_by_seed_key(self, seed_key: str) -> TrailRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, title, description, seed_key, merged_material, material_preview, created_at, updated_at
                FROM trails
                WHERE seed_key = ?
                """,
                (seed_key,),
            ).fetchone()
        return TrailRecord(**dict(row)) if row else None

    def get_trail_documents(self, trail_id: str) -> list[TrailDocumentRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, trail_id, filename, mime_type, content_text, created_at
                FROM trail_documents
                WHERE trail_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (trail_id,),
            ).fetchall()
        return [TrailDocumentRecord(**dict(row)) for row in rows]

    def clear_trail_documents(self, trail_id: str):
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                DELETE FROM trail_documents
                WHERE trail_id = ?
                """,
                (trail_id,),
            )
            connection.commit()
