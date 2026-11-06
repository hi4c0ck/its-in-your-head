"""Backend routes — mounted at /api/plugins/its-in-your-head/ inside the gateway.

Read-only views over the plugin's WAL SQLite store. The desktop half reaches
these through ctx.rest (namespace-scoped, traversal rejected); gateway-wide
data should use host.request instead.
"""
from fastapi import APIRouter, Query

router = APIRouter()


def _db():
    from plugins.plugin_storage import plugin_db
    return plugin_db("its-in-your-head")


@router.get("/metrics")
async def metrics():
    conn = _db()
    total, tokens = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_tokens), 0) FROM events"
    ).fetchone()
    last_ts = conn.execute("SELECT MAX(ts) FROM events").fetchone()[0]
    return {"events": total, "total_tokens": int(tokens or 0), "last_event": last_ts}


@router.get("/heatmap")
async def heatmap(window: str = Query("24h")):
    """Hourly buckets for the strip's heat canvas: call volume + error count."""
    import time as _t
    hours = {"1h": 1, "24h": 24, "7d": 168}.get(window, 24)
    since = _t.time() - hours * 3600
    conn = _db()
    rows = conn.execute(
        """SELECT (ts - ?) / 3600, type, COUNT(*)
           FROM events WHERE ts >= ? GROUP BY 1, 2""",
        (since, since),
    ).fetchall()
    cells = {}
    for hour, etype, n in rows:
        c = cells.setdefault(int(hour), {"calls": 0, "errors": 0, "turns": 0})
        key = {"api.call": "calls", "api.error": "errors", "turn.start": "turns"}.get(etype, "calls")
        c[key] += n
    return {"hours": hours, "cells": {str(k): v for k, v in sorted(cells.items())}}


@router.get("/recent")
async def recent(limit: int = Query(12, le=100)):
    conn = _db()
    rows = conn.execute(
        """SELECT type, ts, model, total_tokens, finish_reason, status_code
           FROM events ORDER BY id DESC LIMIT ?""", (limit,),
    ).fetchall()
    return [
        {"type": r[0], "ts": r[1], "model": r[2], "total_tokens": r[3],
         "finish_reason": r[4], "status_code": r[5]}
        for r in rows
    ]
