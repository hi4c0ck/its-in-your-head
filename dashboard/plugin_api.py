"""Backend routes — OWNER implements; stubs fix the response shapes.

Infra: this FastAPI router is what the desktop half reaches via ctx.rest —
mounted at /api/plugins/its-in-your-head/ inside the gateway process
(namespace-scoped; traversal to other plugins or core routes is structurally
rejected). Backend code runs in-gateway, so it can import hermes internals.

First-use rule: the dashboard mount CANNOT rely on the metrics worker's lazy
connection having run, and must not import the worker module — bootstrap the
schema in-route (CREATE TABLE IF NOT EXISTS ...; keep DDL in sync with
metrics.py and mark the source of truth).
"""
from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/metrics")
async def metrics():
    """OWNER: aggregate totals. Response shape (contract for the desktop half):
    {"events": int, "total_tokens": int, "last_event": float | None}"""
    return {"events": 0, "total_tokens": 0, "last_event": None}


@router.get("/heatmap")
async def heatmap(window: str = Query("24h")):
    """OWNER: hourly cells for the heat canvas. window in {"1h","24h","7d"}.
    Response shape: {"hours": int, "cells": {"<hour_index>":
    {"turns": int, "calls": int, "errors": int}}}"""
    hours = {"1h": 1, "24h": 24, "7d": 168}.get(window, 24)
    return {"hours": hours, "cells": {}}


@router.get("/recent")
async def recent(limit: int = Query(12, le=100)):
    """OWNER: last N I/O frames. Response shape: list of
    {"type", "ts", "model", "total_tokens", "finish_reason", "status_code"}"""
    return []
