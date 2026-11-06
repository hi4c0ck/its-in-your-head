"""Backend routes — the desktop half's ctx.rest target.

Infra: this FastAPI router is what the desktop half reaches via ctx.rest —
mounted at /api/plugins/its-in-your-head/ inside the gateway process
(namespace-scoped; traversal to other plugins or core routes is structurally
rejected). Backend code runs in-gateway, so it can import hermes internals.

First-use rule (doc pitfall): the dashboard mount CANNOT rely on the metrics
worker's lazy connection having run, and it must not import the worker
module — the schema is bootstrapped in-route. _DDL below is an alias of
storage.migration.EVENTS_DDL (single source; the dashboard imports only
storage, never the worker). P2: DB path resolution/connect goes through
storage.database exclusively — no dashboard-local override state.

Error posture: DB unavailable / table absent / write-locked -> routes return
the documented empty shapes (zeros, empty cells, []) so the desktop half
degrades to "no data", it never 500s.
"""
import importlib
import sqlite3
import time
from pathlib import Path

try:  # namespaced gateway import vs top-level (tests: dashboard.plugin_api)
    from ..storage import database as _database
    from ..storage import migration as _migration
except ImportError:
    from storage import database as _database
    from storage import migration as _migration

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

PLUGIN_ID = "its-in-your-head"

# Single canonical DDL lives in storage.migration; _DDL is an alias kept for
# existing callers/tests. Do not copy the DDL here.
_DDL = _migration.EVENTS_DDL

router = APIRouter()


def set_db_path(path: "Path | str") -> None:
    """TEST ONLY: point the routes at a local file instead of plugin_db().
    Forwards to storage.database.set_override — the single override (P2);
    the worker/storage connect() then targets the same file."""
    _database.set_override(path)


_profile_path_override = None  # test-only, see set_profile_path()


def set_profile_path(path) -> None:
    """TEST ONLY: point /context source detection at a fixture profile
    root (or None to clear). Mirrors set_db_path: no global state is
    consulted when this is set, so fixture profiles stay hermetic."""
    global _profile_path_override
    _profile_path_override = None if path is None else Path(path)


def _ctx_profile_root(ctx):
    """Profile root exposed by a ctx double / real ctx, or None.

    Probes the attribute/method names a Hermes ctx may carry
    (profile_dir/profile_home/hermes_home/home and getters). A ctx that
    exposes none of them yields None — the caller degrades to 'unknown',
    never to a guessed present."""
    if ctx is None:
        return None
    for name in ("profile_dir", "profile_home", "profile_path",
                 "profile_root", "hermes_home", "hermes_home_path",
                 "home_dir", "home"):
        try:
            value = getattr(ctx, name, None)
        except Exception:
            continue
        if value is None:
            continue
        try:
            if callable(value):
                value = value()
            if value:
                return Path(str(value))
        except Exception:
            continue
    for name in ("get_profile_dir", "get_profile_home", "get_hermes_home",
                 "profile_dir_path"):
        try:
            fn = getattr(ctx, name, None)
            if callable(fn):
                value = fn()
                if value:
                    return Path(str(value))
        except Exception:
            continue
    return None


def _resolve_profile_root():
    """Active profile (Hermes home) root at request time, or None.

    Order: test override, then ctx-exposed path (the route-context
    resolution the other routes use via set_ctx), then — only when no
    ctx is wired at all — the same guarded gateway resolvers the rest
    of the plugin uses (plugins.plugin_storage.plugin_data_dir, whose
    <home>/plugin-data/<id> location implies the home, and
    hermes_constants.get_hermes_home). When a ctx IS wired but exposes
    no profile path (test doubles), resolution is unavailable -> None
    -> 'unknown'. No Path.home()-style guessing."""
    if _profile_path_override is not None:
        return _profile_path_override
    ctx = _get_ctx()
    root = _ctx_profile_root(ctx)
    if root is not None:
        return root
    if ctx is not None:
        return None
    try:
        from plugins.plugin_storage import plugin_data_dir
        data_dir = Path(plugin_data_dir(PLUGIN_ID))
        if data_dir.parent.name == "plugin-data":
            return data_dir.parent.parent
        return data_dir.parent
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return None


def _detect_state(candidates):
    """'present' if any candidate exists, 'absent' if the profile root
    resolved but none do, 'unknown' if it did not resolve."""
    root = _resolve_profile_root()
    if root is None:
        return "unknown"
    for rel in candidates:
        try:
            if (root / rel).exists():
                return "present"
        except Exception:
            continue
    return "absent"


# Filesystem-backed /context rows: item -> candidate paths relative to
# the profile root (Hermes home). Real layout: SOUL.md/AGENTS.md at the
# root, USER.md/MEMORY.md under memories/, skills under skills/.
_CONTEXT_FS_ROWS = {
    "SOUL.md": ("SOUL.md",),
    "USER.md": ("memories/USER.md", "USER.md"),
    "MEMORY.md": ("memories/MEMORY.md", "MEMORY.md"),
    "AGENTS.md": ("AGENTS.md",),
    "active skills": ("skills",),
}


def _connect() -> sqlite3.Connection:
    # Lazy, per-request via the single storage resolver: fresh WAL
    # connection each time (never cached — one open handle would outlive
    # the gateway's lifetime in ways the lazy rule forbids). Migration runs
    # in-route via schema_bootstrap, never at register/import time.
    return _database.connect()


def _query(sql: str, params: tuple = ()):
    """Read query; rows, or None on any failure (DB missing, not yet
    bootstrapped, locked). Callers map None to the empty shape."""
    try:
        conn = _connect()
    except Exception:
        return None
    try:
        _migration.schema_bootstrap(conn, _DDL)  # in-route migrate/bootstrap
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
    {"type", "ts", "model", "total_tokens", "finish_reason", "status_code",
    "tool", "tool_name", "status_word"}, newest first.

    tool/tool_name + status_word are additive (C1: additive keys allowed;
    S6 desktop ticker/pet phrasing needs them). status_word is a public
    word only (ok/fail/running) — never raw reasoning or tool output.
    """
    n = max(1, limit)
    rows = _query(
        "SELECT type, ts, model, total_tokens, finish_reason, status_code, "
        "tool_name, status "
        "FROM events ORDER BY id DESC LIMIT ?", (n,))
    if rows is None:
        return []
    return [_recent_row(r) for r in rows]


def _status_word(etype, status_code, status):
    """Public status word for the ticker/pet (never payload text)."""
    st = str(status or "").lower()
    if etype == "error" or st in ("error", "failed", "fail") or (
            isinstance(status_code, (int, float)) and status_code >= 400):
        return "fail"
    if etype == "tool_start" or st in ("running", "started", "pending"):
        return "running"
    return "ok"


def _recent_row(r):
    tool_name = r[6] if len(r) > 6 else None
    status = r[7] if len(r) > 7 else None
    return {
        "type": r[0], "ts": r[1], "model": r[2],
        "total_tokens": r[3], "finish_reason": r[4], "status_code": r[5],
        "tool": tool_name, "tool_name": tool_name,
        "status_word": _status_word(r[0], r[5], status),
    }


# ---------------------------------------------------------------------------
# S5 routes (C1 planned additions). All engine imports are guarded: S3
# (perception.portrait) and S4 (pulse.matrix) land in parallel lanes, so
# missing modules yield the documented empty shapes, never a 500.
# Sync helpers are shared with commands.py (the /mirror surface).
# ---------------------------------------------------------------------------

def _dimensions():
    try:
        try:
            from perception.anchors import DIMENSIONS
        except Exception:
            from ..perception.anchors import DIMENSIONS  # type: ignore
        return tuple(DIMENSIONS)
    except Exception:
        return ("directness", "context_richness", "exploration",
                "analytical_depth", "iteration", "scope")


def _settings_defaults():
    """C4 key set + defaults, read from core/settings.py DEFAULTS."""
    try:
        try:
            from core.settings import DEFAULTS
        except Exception:
            from ..core.settings import DEFAULTS  # type: ignore
        return dict(DEFAULTS)
    except Exception:
        return {
            "perception.enabled": True,
            "observation.reasoning": True,
            "observation.retain_days": 90,
            "pulse.prototype_cap": 64,
            "pulse.min_labeled": 20,
            "pulse.thresholds.goal": 0.8,
            "pulse.thresholds.good": 0.6,
            "pulse.thresholds.friction": 0.4,
            "adaptation.enabled": False,
            "adaptation.rules": [],
        }


_ctx = None  # plugin ctx, set by register() via set_ctx (gateway wiring)
_ephemeral_settings: dict = {}  # accepted-but-not-persisted values


def set_ctx(ctx) -> None:
    """GATEWAY WIRING (test-only until __init__ seam lands): remember the
    plugin ctx so /settings can read/write config via its config API."""
    global _ctx
    _ctx = ctx


def _get_ctx():
    return _ctx


def _settings_current() -> dict:
    """Current C4 values: ctx config wins, then ephemeral, then DEFAULTS."""
    defaults = _settings_defaults()
    out = dict(defaults)
    out.update({k: v for k, v in _ephemeral_settings.items() if k in defaults})
    ctx = _get_ctx()
    get = getattr(ctx, "get_config", None)
    if callable(get):
        for key, default in defaults.items():
            try:
                out[key] = get(key, default)
            except Exception:
                pass
    return out


def _engine_first(module_names, fn_names):
    """First importable (module, callable) pair, or (None, None). No I/O
    beyond the import itself; callers wrap the call in try/except."""
    for mod_name in module_names:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for fn_name in fn_names:
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                return mod, fn
    return None, None


_PULSE_COLUMNS = ["goal", "good", "friction", "unlabeled"]


def _pulse_shape() -> dict:
    """Matrix rows x 4 columns (S4 pulse.matrix when importable)."""
    shape = {"rows": [], "columns": list(_PULSE_COLUMNS), "available": False}
    _, fn = _engine_first(
        ("pulse.matrix", "pulse"),
        ("matrix", "get_matrix", "build_matrix", "compute_matrix"))
    if fn is None:
        return shape
    try:
        result = fn()
    except Exception:
        return shape
    try:
        if isinstance(result, dict):
            rows = result.get("rows", [])
            cols = result.get("columns", _PULSE_COLUMNS)
            shape.update({"rows": list(rows), "columns": list(cols),
                          "available": True})
        elif isinstance(result, (list, tuple)):
            shape.update({"rows": list(result), "available": True})
    except Exception:
        return {"rows": [], "columns": list(_PULSE_COLUMNS), "available": False}
    return shape


def _portrait_shape() -> dict:
    """Six dimension values + per-dim confidence + user/agent split
    (S3 perception.portrait when importable)."""
    dims = _dimensions()
    shape = {
        "available": False,
        "dimensions": {d: None for d in dims},
        "confidence": {d: None for d in dims},
        "user": {d: None for d in dims},
        "agent": {d: None for d in dims},
    }
    _, fn = _engine_first(
        ("perception.portrait", "perception.portrait_store"),
        ("portrait", "get_portrait", "build_portrait", "compute_portrait",
         "current_portrait", "load_portrait", "portrait_for_text"))
    if fn is None:
        return shape
    try:
        result = fn()
    except TypeError:
        return shape  # needs arguments (e.g. text) — no stored portrait yet
    except Exception:
        return shape
    if not isinstance(result, dict):
        return shape
    try:
        dim_src = result.get("dimensions") or result.get("resemblance") \
            or result.get("values") or {}
        conf_src = result.get("confidence") or {}
        user_src = result.get("user") or {}
        agent_src = result.get("agent") or {}
        for d in dims:
            if isinstance(dim_src, dict) and dim_src.get(d) is not None:
                shape["dimensions"][d] = dim_src[d]
            if isinstance(conf_src, dict) and conf_src.get(d) is not None:
                shape["confidence"][d] = conf_src[d]
            if isinstance(user_src, dict) and user_src.get(d) is not None:
                shape["user"][d] = user_src[d]
            if isinstance(agent_src, dict) and agent_src.get(d) is not None:
                shape["agent"][d] = agent_src[d]
        shape["available"] = any(
            v is not None for v in shape["dimensions"].values())
    except Exception:
        return shape
    return shape


def _drift_shape() -> dict:
    """Per-dim time-series sparkline arrays from stored portrait history.
    Empty arrays until a portrait_history table exists (S3/S7 fill it)."""
    dims = _dimensions()
    shape = {"series": {d: [] for d in dims}, "available": False}
    try:
        conn = _connect()
        try:
            _migration.schema_bootstrap(conn, _DDL)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='portrait_history'").fetchone()
            if row is None:
                return shape
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(portrait_history)").fetchall()]
            present = [d for d in dims if d in cols]
            if not present:
                return shape
            order = "ts" if "ts" in cols else "rowid"
            rows = conn.execute(
                f"SELECT {', '.join(present)} FROM portrait_history "
                f"ORDER BY {order}").fetchall()
            idx = {d: present.index(d) for d in present}
            for d in present:
                shape["series"][d] = [
                    r[idx[d]] for r in rows if r[idx[d]] is not None]
            shape["available"] = True
        finally:
            conn.close()
    except Exception:
        return {"series": {d: [] for d in dims}, "available": False}
    return shape


_CONTEXT_ROWS = [
    {"item": "user message", "visibility": "VISIBLE",
     "source": "pre_llm_call hook", "present": True},
    {"item": "history extent", "visibility": "KNOWN",
     "source": "conversation history", "present": True},
    {"item": "system instructions", "visibility": "NOT EXPOSED",
     "source": None, "present": False},
    # Filesystem-backed rows: present/state are recomputed at request
    # time in _context_shape() from the actual profile (see
    # _CONTEXT_FS_ROWS); the values here are never reported as-is.
    {"item": "SOUL.md", "visibility": "KNOWN",
     "source": "SOUL.md", "present": False},
    {"item": "USER.md", "visibility": "KNOWN",
     "source": "USER.md", "present": False},
    {"item": "MEMORY.md", "visibility": "KNOWN",
     "source": "MEMORY.md", "present": False},
    {"item": "AGENTS.md", "visibility": "KNOWN",
     "source": "AGENTS.md", "present": False},
    {"item": "active skills", "visibility": "KNOWN",
     "source": "skill registry", "present": False},
    {"item": "plugin adaptation", "visibility": "KNOWN",
     "source": "adaptation.enabled (S7 engine)", "present": False},
]


def _context_shape() -> dict:
    """Provenance table; S7 fills the adaptation rows dynamically.
    Filesystem rows carry a request-time detected state
    (present/absent/unknown); absent rows stay in the table."""
    rows = [dict(r) for r in _CONTEXT_ROWS]
    for row in rows:
        if row.get("item") in _CONTEXT_FS_ROWS:
            state = _detect_state(_CONTEXT_FS_ROWS[row["item"]])
            row["state"] = state
            row["present"] = state == "present"
        else:
            row["state"] = "present" if row.get("present") else "absent"
    try:
        enabled = bool(_settings_current().get("adaptation.enabled", False))
    except Exception:
        enabled = False
    last = None
    try:
        try:
            from adaptation import injection as _inj
        except Exception:
            from ..adaptation import injection as _inj  # type: ignore
        last = _inj.last_application()
    except Exception:
        last = None
    for row in rows:
        if row.get("item") == "plugin adaptation":
            row["present"] = enabled
            row["state"] = "present" if enabled else "absent"
            if enabled:
                row["source"] = "adaptation engine"
    rows.append({
        "item": "injected instruction",
        "visibility": "VISIBLE" if last is not None else "KNOWN",
        "source": "pre_llm_call context injection; may persist in API sidecar",
        "present": last is not None,
        "state": "present" if last is not None else "absent",
    })
    return {"rows": rows}


def _reset_dataset() -> dict:
    """Wipe the plugin-data analytical dataset (events + portrait history
    if present). Never touches config. Idempotent; returns counts."""
    counts = {"events": 0, "portrait_history": 0}
    try:
        conn = _connect()
    except Exception:
        return {"wiped": counts, "reset": True}
    try:
        _migration.schema_bootstrap(conn, _DDL)
        cur = conn.execute("DELETE FROM events")
        counts["events"] = cur.rowcount if cur.rowcount is not None else 0
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='portrait_history'").fetchone()
        if row is not None:
            cur = conn.execute("DELETE FROM portrait_history")
            counts["portrait_history"] = (
                cur.rowcount if cur.rowcount is not None else 0)
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"wiped": counts, "reset": True}


@router.get("/pulse")
async def pulse():
    """Pulse matrix (rows x 4 columns): live engine data when available,
    else the import-guarded shape probe (S5)."""
    try:
        import importlib
        eng = importlib.import_module("engine")
        return eng.get_engine().pulse_view()
    except Exception:
        return _pulse_shape()


@router.get("/portrait")
async def portrait():
    """Portrait: six dimensions + confidence + user/agent split.
    Live engine data when available, else the shape probe (S5)."""
    try:
        import importlib
        eng = importlib.import_module("engine")
        view = eng.get_engine().portrait_view()
        if any(v is not None for v in view["dimensions"].values()):
            return view
    except Exception:
        pass
    return _portrait_shape()


@router.get("/drift")
async def drift():
    """Drift: per-dim sparkline arrays. In-memory engine history first,
    then the portrait_history table probe (S5), else empty shape."""
    try:
        import importlib
        eng = importlib.import_module("engine")
        view = eng.get_engine().drift_view()
        if view["available"]:
            return view
    except Exception:
        pass
    return _drift_shape()


@router.get("/context")
async def context():
    """Provenance table shell (static model; S7 fills it)."""
    return _context_shape()


@router.get("/settings")
async def settings():
    """C4 keys + current values (ctx config, else DEFAULTS). The flat
    dotted-key map lives under "settings"/"keys"; the nested top-level
    expansion is what the desktop settingValue() walk resolves."""
    flat = _settings_current()
    nested: dict = {}
    for key, value in flat.items():
        cur = nested
        parts = key.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
    return {**nested, "settings": flat, "keys": sorted(flat)}


@router.post("/settings")
async def update_settings(request: Request):
    """Validate against the C4 key set; unknown keys -> 400. Persists via
    ctx.set_config when available, else accepted-but-ephemeral (flagged)."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    incoming = payload.get("settings") if isinstance(
        payload.get("settings"), dict) else payload
    defaults = _settings_defaults()
    unknown = sorted(k for k in incoming if k not in defaults)
    if unknown:
        return JSONResponse(
            status_code=400,
            content={"error": "unknown settings keys", "unknown": unknown})
    accepted = {k: incoming[k] for k in incoming if k in defaults}
    ctx = _get_ctx()
    setter = getattr(ctx, "set_config", None)
    persisted = False
    if callable(setter):
        try:
            for key, value in accepted.items():
                setter(key, value)
            persisted = True
        except Exception:
            persisted = False
    if not persisted:
        _ephemeral_settings.update(accepted)
    return {
        "settings": _settings_current(),
        "accepted": accepted,
        "persisted": persisted,
        "ephemeral": not persisted,
    }


@router.post("/reset")
async def reset():
    """Explicit wipe of the plugin-data analytical dataset (NOT config)."""
    out = _reset_dataset()
    try:
        import importlib
        importlib.import_module("engine").reset_engine()
    except Exception:
        pass  # process view stays stale until restart; DB wipe is the fact
    return out
