"""
backend/db.py
-------------
Async SQLite session store for GenomeUI.

Replaces the flat-file sessions.json with per-session rows in WAL mode.
Each session is stored as a single JSON blob keyed by session ID.
Atomic upsert per session with exact-set sync semantics for full saves.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from typing import Any

import aiosqlite

_log = logging.getLogger("genomeui.db")

_DEFAULT_DB_PATH = pathlib.Path(__file__).parent / "data" / "sessions.db"
_DB_PATH = pathlib.Path(os.getenv("GENOMEUI_SESSION_DB_PATH", str(_DEFAULT_DB_PATH)))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT    PRIMARY KEY,
    state      TEXT    NOT NULL,
    revision   INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scheduler_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT    NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 0,
    detail     TEXT    NOT NULL DEFAULT '',
    data       TEXT    NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_scheduler_runs_task_created
ON scheduler_runs(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS paired_surfaces (
    surface_id  TEXT    PRIMARY KEY,
    profile_id  TEXT    NOT NULL DEFAULT 'default',
    platform    TEXT    NOT NULL DEFAULT '',
    role        TEXT    NOT NULL DEFAULT '',
    label       TEXT    NOT NULL DEFAULT '',
    did         TEXT    NOT NULL DEFAULT '',
    relay_url   TEXT    NOT NULL DEFAULT '',
    push_tokens TEXT    NOT NULL DEFAULT '{}',
    state       TEXT    NOT NULL DEFAULT '{}',
    updated_at  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_paired_surfaces_profile_updated
ON paired_surfaces(profile_id, updated_at DESC);
"""

_conn: aiosqlite.Connection | None = None
_last_write_ms = 0


def get_db_path() -> pathlib.Path:
    return _DB_PATH


async def init(path: str | pathlib.Path | None = None) -> None:
    """Open (or create) the sessions database. Call once at startup."""
    global _conn, _DB_PATH
    if path is not None:
        _DB_PATH = pathlib.Path(path)
    if _conn is not None:
        await _conn.close()
        _conn = None
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(str(_DB_PATH))
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA synchronous=NORMAL")
    await _conn.executescript(_SCHEMA)
    await _conn.commit()
    _log.info("Session DB opened: %s", _DB_PATH)


async def close() -> None:
    """Close the database connection. Call once at shutdown."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _require() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("db.init() was not called before using the session store")
    return _conn


def _encode_state(state: dict[str, Any]) -> tuple[str, int, int]:
    global _last_write_ms
    now = int(time.time() * 1000)
    if now <= _last_write_ms:
        now = _last_write_ms + 1
    _last_write_ms = now
    return (
        json.dumps(state, separators=(",", ":")),
        int(state.get("revision") or 0),
        now,
    )


async def save_session(session_id: str, state: dict[str, Any]) -> None:
    """Atomically upsert a single session row."""
    db = _require()
    row_state, revision, updated_at = _encode_state(state)
    await db.execute(
        "INSERT INTO sessions (id, state, revision, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET"
        "   state      = excluded.state,"
        "   revision   = excluded.revision,"
        "   updated_at = excluded.updated_at",
        (str(session_id), row_state, revision, updated_at),
    )
    await db.commit()


async def load_session(session_id: str) -> dict[str, Any] | None:
    """Load one session by ID."""
    db = _require()
    cursor = await db.execute(
        "SELECT state FROM sessions WHERE id = ?",
        (str(session_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except Exception as exc:
        _log.warning("Skipping corrupt session row %s: %s", session_id, exc)
        return None


async def delete_session(session_id: str) -> None:
    """Delete one session row if present."""
    db = _require()
    await db.execute("DELETE FROM sessions WHERE id = ?", (str(session_id),))
    await db.commit()


async def save_all(sessions: dict[str, dict[str, Any]]) -> None:
    """Replace the on-disk session set with the provided in-memory snapshot."""
    db = _require()
    session_ids = [str(sid) for sid in sessions.keys()]
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        await db.execute(
            f"DELETE FROM sessions WHERE id NOT IN ({placeholders})",
            session_ids,
        )
    else:
        await db.execute("DELETE FROM sessions")
    rows = []
    for sid, state in sessions.items():
        row_state, revision, updated_at = _encode_state(state)
        rows.append((str(sid), row_state, revision, updated_at))
    if rows:
        await db.executemany(
            "INSERT INTO sessions (id, state, revision, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "   state      = excluded.state,"
            "   revision   = excluded.revision,"
            "   updated_at = excluded.updated_at",
            rows,
        )
    await db.commit()


async def load_all() -> dict[str, dict[str, Any]]:
    """Load all sessions ordered by last update. Returns {id: state_dict}."""
    db = _require()
    cursor = await db.execute(
        "SELECT id, state FROM sessions ORDER BY updated_at DESC"
    )
    rows = await cursor.fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row_id, row_state in rows:
        try:
            result[str(row_id)] = json.loads(row_state)
        except Exception as exc:
            _log.warning("Skipping corrupt session row %s: %s", row_id, exc)
    return result


async def list_sessions() -> list[str]:
    """Return session IDs ordered by most-recently-updated."""
    db = _require()
    cursor = await db.execute(
        "SELECT id FROM sessions ORDER BY updated_at DESC"
    )
    rows = await cursor.fetchall()
    return [str(r[0]) for r in rows]


async def list_ids() -> list[str]:
    """Backward-compatible alias for callers that still use list_ids()."""
    return await list_sessions()


async def save_scheduler_run(
    task_id: str,
    ok: bool,
    detail: str,
    data: dict[str, Any] | None = None,
    created_at: int | None = None,
) -> None:
    """Persist one scheduler task result row and trim older history."""
    db = _require()
    ts = int(created_at or 0) or max(int(time.time() * 1000), _last_write_ms + 1)
    payload = json.dumps(data or {}, separators=(",", ":"))
    await db.execute(
        "INSERT INTO scheduler_runs (task_id, ok, detail, data, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(task_id), 1 if ok else 0, str(detail or "")[:200], payload, ts),
    )
    await db.execute(
        "DELETE FROM scheduler_runs WHERE id NOT IN ("
        "  SELECT id FROM scheduler_runs ORDER BY created_at DESC LIMIT 500"
        ")"
    )
    await db.commit()


async def list_scheduler_runs(task_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent scheduler task results, newest first."""
    db = _require()
    if task_id:
        cursor = await db.execute(
            "SELECT task_id, ok, detail, data, created_at"
            " FROM scheduler_runs WHERE task_id = ?"
            " ORDER BY created_at DESC LIMIT ?",
            (str(task_id), max(1, int(limit))),
        )
    else:
        cursor = await db.execute(
            "SELECT task_id, ok, detail, data, created_at"
            " FROM scheduler_runs ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
    rows = await cursor.fetchall()
    results: list[dict[str, Any]] = []
    for row_task_id, row_ok, row_detail, row_data, row_created_at in rows:
        try:
            parsed_data = json.loads(row_data or "{}")
        except Exception:
            parsed_data = {}
        results.append({
            "task_id": str(row_task_id),
            "ts": int(row_created_at or 0),
            "ok": bool(row_ok),
            "detail": str(row_detail or ""),
            "data": parsed_data if isinstance(parsed_data, dict) else {},
        })
    return results


async def save_paired_surface(
    surface_id: str,
    profile_id: str,
    state: dict[str, Any],
) -> None:
    """Atomically upsert one paired surface descriptor."""
    db = _require()
    now = int(time.time() * 1000)
    push_tokens = state.get("pushTokens", {}) if isinstance(state.get("pushTokens"), dict) else {}
    payload = json.dumps(state, separators=(",", ":"))
    push_payload = json.dumps(push_tokens, separators=(",", ":"))
    await db.execute(
        "INSERT INTO paired_surfaces (surface_id, profile_id, platform, role, label, did, relay_url, push_tokens, state, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(surface_id) DO UPDATE SET"
        "   profile_id  = excluded.profile_id,"
        "   platform    = excluded.platform,"
        "   role        = excluded.role,"
        "   label       = excluded.label,"
        "   did         = excluded.did,"
        "   relay_url   = excluded.relay_url,"
        "   push_tokens = excluded.push_tokens,"
        "   state       = excluded.state,"
        "   updated_at  = excluded.updated_at",
        (
            str(surface_id),
            str(profile_id or "default"),
            str(state.get("platform", "") or "")[:64],
            str(state.get("role", "") or "")[:64],
            str(state.get("label", "") or "")[:160],
            str(state.get("did", "") or "")[:160],
            str(state.get("relayUrl", "") or "")[:512],
            push_payload,
            payload,
            now,
        ),
    )
    await db.commit()


async def load_paired_surface(surface_id: str) -> dict[str, Any] | None:
    """Load one paired surface by ID."""
    db = _require()
    cursor = await db.execute(
        "SELECT state FROM paired_surfaces WHERE surface_id = ?",
        (str(surface_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except Exception as exc:
        _log.warning("Skipping corrupt paired surface row %s: %s", surface_id, exc)
        return None


async def list_paired_surfaces(profile_id: str = "default", limit: int = 50) -> list[dict[str, Any]]:
    """Return paired surfaces for one profile, newest first."""
    db = _require()
    cursor = await db.execute(
        "SELECT state FROM paired_surfaces WHERE profile_id = ? ORDER BY updated_at DESC LIMIT ?",
        (str(profile_id or "default"), max(1, int(limit))),
    )
    rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for (row_state,) in rows:
        try:
            parsed = json.loads(row_state)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            out.append(parsed)
    return out
