"""Backend routes — the desktop half's ctx.rest target.

Infra: this FastAPI router is what the desktop half reaches via ctx.rest —
mounted at /api/plugins/its-in-your-head/ inside the gateway process
(namespace-scoped; traversal to other plugins or core routes is structurally
rejected). Backend code runs in-gateway, so it can import hermes internals.

First-use rule (doc pitfall): the dashboard mount CANNOT rely on the metrics
worker's lazy connection having run, and it must not import the worker
module — the schema is bootstrapped in-route. _DDL below is deliberately a
copy of metrics.SCHEMA (source of truth: metrics.py) — keep the two in sync.

Error posture: DB unavailable / table absent / write-locked -> routes return
the documented empty shapes (zeros, empty cells, []) so the desktop half
degrades to "no data", it never 500s.
"""
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Query

PLUGIN_ID = "its-in-your-head"

# Deliberate copy of metrics.SCHEMA (source of truth: metrics.py) — the
# dashboard mount must not import the worker module. Keep in sync.
_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY,
    ts                REAL NOT NULL,
    type              TEXT NOT NULL,
    session_id        TEXT,
    turn_id           TEXT,
    api_request_id    TEXT,
    model             TEXT,
    provider          TEXT,
    api_duration      REAL,
    finish_reason     TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    status_code       INTEGER,
    retry_count       INTEGER,
    retryable         INTEGER,
    error_type        TEXT,
    error_message     TEXT,
    user_chars        INTEGER,
    user_message      TEXT,
    sketch            BLOB,
    tool_name         TEXT,
    task_id           TEXT,
    tool_call_id      TEXT,
    duration_ms       REAL,
    result_type       TEXT,
    result_chars      INTEGER,
    status            TEXT,
    finished          INTEGER,
    reasoning_chars   INTEGER,
    reasoning_deltas  INTEGER,
    final_chars       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);
CREATE INDEX IF NOT EXISTS idx_events_tool ON events (tool_name, ts);
"""

router = APIRouter()

_db_path_override: "Path | str | None" = None  # test-only


def set_db_path(path: "Path | str") -> None:
    """TEST ONLY: point the routes at a local file instead of plugin_db()."""
    global _db_path_override
    _db_path_override = path


def _connect() -> sqlite3.Connection:
    if _db_path_override is not None:
        conn = sqlite3.connect(str(_db_path_override))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    # Lazy, per-request: plugin_db hands back a fresh WAL connection with
    # check_same_thread=False; the dashboard reader and the worker writer
    # coexist because WAL. Never cached — one open handle would outlive the
    # gateway's lifetime in ways the lazy rule forbids.
    from plugins.plugin_storage import plugin_db
    return plugin_db(PLUGIN_ID)


def _query(sql: str, params: tuple = ()):
    """Read query; rows, or None on any failure (DB missing, not yet
    bootstrapped, locked). Callers map None to the empty shape."""
    try:
        conn = _connect()
    except Exception:
        return None
    try:
        conn.executescript(_DDL)  # in-route bootstrap (IF NOT EXISTS)
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


@router.get("/metrics")
async def metrics():
    """Aggregate totals. Response shape (contract for the desktop half):
    {"events": int, "total_tokens": int, "last_event": float | None}"""
    rows = _query(
        "SELECT COUNT(*), COALESCE(SUM(total_tokens), 0), MAX(ts) FROM events")
    if rows is None:
        return {"events": 0, "total_tokens": 0, "last_event": None}
    (events, total_tokens, last_event), = rows
    return {"events": events, "total_tokens": total_tokens, "last_event": last_event}


@router.get("/heatmap")
async def heatmap(window: str = Query("24h")):
    """Hourly cells for the heat canvas. window in {"1h","24h","7d"}.
    Response shape: {"hours": int, "cells": {"<hour_index>":
    {"turns": int, "calls": int, "errors": int}}} — hour_index 0 is the
    current hour, hours-1 the oldest in the window; keys are strings (JSON)."""
    hours = {"1h": 1, "24h": 24, "7d": 168}.get(window, 24)
    cutoff = time.time() - hours * 3600
    rows = _query(
        "SELECT ts, type FROM events WHERE ts >= ? ORDER BY ts", (cutoff,))
    if rows is None:
        return {"hours": hours, "cells": {}}
    now = time.time()
    cells: dict = {}
    for ts, etype in rows:
        idx = min(hours - 1, max(0, int((now - ts) // 3600)))
        cell = cells.setdefault(idx, {"turns": 0, "calls": 0, "errors": 0})
        key = {"turn": "turns", "api": "calls", "error": "errors"}.get(etype)
        if key:
            cell[key] += 1
    return {"hours": hours, "cells": {str(i): c for i, c in cells.items()}}


@router.get("/recent")
async def recent(limit: int = Query(12, le=100)):
    """Last N I/O frames. Response shape: list of
    {"type", "ts", "model", "total_tokens", "finish_reason", "status_code"},
    newest first."""
    n = max(1, limit)
    rows = _query(
        "SELECT type, ts, model, total_tokens, finish_reason, status_code "
        "FROM events ORDER BY id DESC LIMIT ?", (n,))
    if rows is None:
        return []
    return [{
        "type": r[0], "ts": r[1], "model": r[2],
        "total_tokens": r[3], "finish_reason": r[4], "status_code": r[5],
    } for r in rows]
