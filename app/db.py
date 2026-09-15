"""SQLite persistence. Three tables, per PLAN.md section 2."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    client_name TEXT,
    spec_json TEXT NOT NULL,
    breakdown_json TEXT,
    total_ils REAL,
    status TEXT NOT NULL DEFAULT 'draft',
    pdf_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_state (
    chat_id INTEGER PRIMARY KEY,
    spec_json TEXT NOT NULL,
    breakdown_json TEXT,
    awaiting_field TEXT,
    awaiting_kind TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcripts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    raw_audio_transcript TEXT,
    parsed_json TEXT,
    correction_text TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quotes_chat ON quotes(chat_id, created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# --- transcripts_log -------------------------------------------------------

def log_interaction(
    chat_id: int,
    transcript: str | None = None,
    parsed: dict[str, Any] | None = None,
    correction_text: str | None = None,
) -> None:
    """Log every interaction from day one — this is the tuning dataset."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO transcripts_log "
            "(chat_id, raw_audio_transcript, parsed_json, correction_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                chat_id,
                transcript,
                json.dumps(parsed, ensure_ascii=False) if parsed else None,
                correction_text,
                _now(),
            ),
        )


# --- pending_state ---------------------------------------------------------

def save_pending(
    chat_id: int,
    spec: dict[str, Any],
    breakdown: dict[str, Any] | None = None,
    awaiting_field: str | None = None,
    awaiting_kind: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO pending_state "
            "(chat_id, spec_json, breakdown_json, awaiting_field, awaiting_kind, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "spec_json=excluded.spec_json, breakdown_json=excluded.breakdown_json, "
            "awaiting_field=excluded.awaiting_field, "
            "awaiting_kind=excluded.awaiting_kind, updated_at=excluded.updated_at",
            (
                chat_id,
                json.dumps(spec, ensure_ascii=False),
                json.dumps(breakdown, ensure_ascii=False) if breakdown else None,
                awaiting_field,
                awaiting_kind,
                _now(),
            ),
        )


def get_pending(chat_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "chat_id": row["chat_id"],
        "spec": json.loads(row["spec_json"]),
        "breakdown": json.loads(row["breakdown_json"]) if row["breakdown_json"] else None,
        "awaiting_field": row["awaiting_field"],
        "awaiting_kind": row["awaiting_kind"],
    }


def clear_pending(chat_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pending_state WHERE chat_id = ?", (chat_id,))


# --- quotes ----------------------------------------------------------------

def create_quote(
    chat_id: int,
    client_name: str | None,
    spec: dict[str, Any],
    breakdown: dict[str, Any],
    total: float,
    status: str = "draft",
) -> int:
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO quotes "
            "(chat_id, client_name, spec_json, breakdown_json, total_ils, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                client_name,
                json.dumps(spec, ensure_ascii=False),
                json.dumps(breakdown, ensure_ascii=False),
                total,
                status,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def mark_quote_approved(quote_id: int, pdf_path: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE quotes SET status='approved', pdf_path=?, updated_at=? WHERE id=?",
            (pdf_path, _now(), quote_id),
        )


def latest_quote(chat_id: int) -> dict[str, Any] | None:
    """Most recent quote for a chat — the seed for 'like the last kitchen'."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM quotes WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)
        ).fetchone()
    return dict(row) if row else None
