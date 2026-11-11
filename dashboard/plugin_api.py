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
import importlib.util
import math
import sqlite3
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

# The plugin-host child loads this file standalone (spec_from_file_location,
# no parent package) with the plugin root OFF its sys.path, so both the
# relative and the bare `storage` import fail there (the dashboard 502:
# "No module named 'storage'"). Build the storage package under a unique
# top-level name from the on-disk layout instead: no sys.path pollution, no
# collision with any other `storage` module in the shared child process.
# Safe because `storage` is stdlib-only and its __init__ uses relative
# imports.
#
# Three load invariants:
# 1. Pre-exec registration: __init__'s relative imports resolve the parent
#    package through sys.modules DURING its own exec, so the package must be
#    registered before exec_module (the child's op_asgi does the same).
# 2. A CROSS-INSTANCE lock: op_asgi registers each concurrent plugin_api
#    instance before its exec, so a post-restart burst of parallel pane
#    polls can exec this file on several instances at once; a per-instance
#    lock cannot serialize them. The lock lives in sys.modules under a
#    private key so every instance of this file shares the same object —
#    a thread that sees the registered name always sees a fully-exec'd
#    module (the 04:40 "has no attribute 'database'" AttributeError 502s).
# 3. Stale-submodule sweep on re-create: the import fast path skips the
#    parent-attr binding for submodules already in sys.modules, so a fresh
#    package must get fresh submodules.
_STORAGE_ALIAS = "hermes_plugin_its_in_your_head_storage"
# Cross-instance lock, shared through sys.modules: every racing exec of this
# file calls setdefault on the same key, and CPython's dict.setdefault is a
# single GIL-atomic operation, so all instances converge on ONE lock object.
# With it, "the registered name exists" always implies "fully-exec'd module".
# (Runtime-legal: sys.modules is a plain dict the import machinery only
# looks up by the names it imports; the private lock key is inert to it. The
# cast bridges the ModuleType annotation for type checkers.)
_storage_load_lock: threading.Lock = cast(
    "dict[str, Any]", sys.modules).setdefault(
    _STORAGE_ALIAS + "_lock", threading.Lock())


def _load_plugin_storage():
    pkg_dir = Path(__file__).resolve().parent.parent / "storage"
    with _storage_load_lock:
        if _STORAGE_ALIAS in sys.modules:
            return sys.modules[_STORAGE_ALIAS]
        for key in [k for k in sys.modules
                    if k.startswith(_STORAGE_ALIAS + ".")]:
            del sys.modules[key]
        spec = importlib.util.spec_from_file_location(
            _STORAGE_ALIAS, pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load plugin storage package at {pkg_dir}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_STORAGE_ALIAS] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            # Never leave a half-initialized or broken package registered: a
            # thread waiting on the lock would otherwise adopt it.
            sys.modules.pop(_STORAGE_ALIAS, None)
            raise
        return module


try:  # namespaced gateway import (agent half loads the plugin as a package)
    from ..storage import database as _database
    from ..storage import migration as _migration
except ImportError:
    try:  # plugin root on sys.path (tests: dashboard.plugin_api alongside
        # bare `storage` — single shared instance with the worker)
        from storage import database as _database
        from storage import migration as _migration
    except ImportError:
        _storage = _load_plugin_storage()
        _database = _storage.database
        _migration = _storage.migration

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


def _pulse_activity_events(after_event_id: int | None = None,
                           limit: int = 256,
                           tool_name: str | None = None) -> dict:
    """Cursor-based PULSE activity batch (v0.1.10 spec §3.3).

    Returns newly persisted tool_start EVENT METADATA only (id, ts,
    tool_name, tool_call_id) — never tool content. Without a cursor the
    database high-water mark is established and NO historical events are
    returned (nothing pre-hydration may animate). With a cursor:
    tool_start events with ids greater than the cursor, ascending event
    id order, one bounded page, has_more on a truncated page.
    Fault-isolated: any DB fault maps to an empty page at the caller's
    cursor — the /pulse route never 500s on activity fetch.

    C/B (additive): ``tool_name`` (when set) restricts the page to ONE
    tool's backlog — the pane's FILO flush-on-visibility reads a tool's
    unconsumed events from the persisted events table (the "external
    toolkit") instead of hoarding them in renderer memory. Same cursor
    semantics (ASC, next_cursor = page high) as the global path; the
    caller reverses the page for newest-first animation. No schema or
    shape change to the response envelope (C1/C3-clean)."""
    limit = max(1, int(limit))
    # Unusable cursor (unresolved FastAPI Query default in direct
    # calls, None, junk) degrades to the baseline: high-water mark,
    # no history — never a fault.
    after = None
    if after_event_id is not None:
        try:
            after = int(after_event_id)
        except (TypeError, ValueError):
            after = None
    # C/B: the tool_name filter must also survive a direct (non-ASGI)
    # call, where the FastAPI Query default is left as the unresolved
    # Query sentinel — only a plain string filters (mirror the `after`
    # guard above so `api.pulse(...)` direct calls stay byte-compatible).
    if not isinstance(tool_name, str):
        tool_name = None
    try:
        conn = _database.connect()
        conn.execute("PRAGMA busy_timeout=2000")  # cross-process WAL reads
    except Exception:
        return {"events": [], "next_cursor": after or 0,
                "has_more": False}
    try:
        _migration.schema_bootstrap(conn, _DDL)
        hi = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
        high_water = int(hi) if hi is not None else 0
        if after is None:
            return {"events": [], "next_cursor": high_water, "has_more": False}
        if tool_name is not None:
            sql = ("SELECT id, ts, tool_name, tool_call_id FROM events"
                   " WHERE id > ? AND type = 'tool_start'"
                   " AND tool_name = ?"
                   " ORDER BY id ASC LIMIT ?")
            params: list = [after, tool_name, limit + 1]
        else:
            sql = ("SELECT id, ts, tool_name, tool_call_id FROM events"
                   " WHERE id > ? AND type = 'tool_start'"
                   " ORDER BY id ASC LIMIT ?")
            params = [after, limit + 1]
        rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "events": [
                {"id": int(r[0]), "ts": r[1], "tool_name": r[2],
                 "tool_call_id": r[3]} for r in page],
            "next_cursor": int(page[-1][0]) if page else after,
            "has_more": has_more,
        }
    except sqlite3.Error:
        return {"events": [], "next_cursor": after or 0,
                "has_more": False}
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
        # LAST RESORT (isolated child, core.settings unimportable): keep
        # these aligned with core.settings.DEFAULTS — the pulse fallback
        # derives row state from them. Mirror the FULL key set; a stale
        # subset here makes /settings POST 400 on any key added to
        # DEFAULTS after this dict was written (that was the encoder
        # switcher's "unknown settings keys" bug).
        return {
            "perception.enabled": True,
            "observation.reasoning": True,
            "observation.retain_days": 90,
            "pulse.prototype_cap": 8,
            "pulse.min_labeled": 3,
            # Per-category readiness minimums (Stage 2 mirror of
            # core.settings.DEFAULTS — None = fall back to min_labeled).
            "pulse.goal_min_references": None,
            "pulse.good_min_samples": None,
            "pulse.user_friction_min_samples": None,
            "pulse.objective_friction_min_samples": None,
            "pulse.thresholds.goal": 0.8,
            "pulse.thresholds.good": 0.6,
            "pulse.thresholds.friction": 0.4,
            "adaptation.enabled": False,
            "adaptation.rules": [],
            "perception.encoder": "tier-0",
            "perception.custom_patterns": [],
            "perception.reinforce": True,
            "perception.reinforce_min_amplification": 0.15,
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

# Cell math mirrors pulse.matrix.build_matrix (which the isolated child
# cannot import — plugin root off its sys.path): cosine / strongest /
# display-floor / level / neutral semantics are pinned identically so the
# fallback rows match the live engine's rows for the same inputs.
_PULSE_DEFAULT_THRESHOLDS = {"goal": 0.8, "good": 0.6, "friction": 0.4}
_PULSE_DEFAULT_MIN_LABELED = 3
# Display guard mirroring the engine's per-tool capsule deque bound
# (engine.CAPSULES_PER_TOOL): the writer prunes, this caps legacy rows.
_PULSE_CAPSULE_DISPLAY_CAP = 32


def _pulse_cosine(a, b) -> float:
    """pulse.prototypes.cosine (mirror): 0.0 for None / length mismatch,
    clamped to [-1, 1]."""
    if a is None or b is None or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot / (na * nb)))


def _pulse_strongest(vector, bank):
    """pulse.prototypes.strongest_similarity (mirror): None when the bank
    is empty — absence of evidence is never a negative score."""
    if vector is None or not bank:
        return None
    return max(_pulse_cosine(vector, proto) for proto in bank)


def _pulse_floor(value):
    """matrix._floor (mirror): display similarity never below 0.0, capped
    at 1.0; None stays None (neutral)."""
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _pulse_level(value, threshold) -> str:
    """matrix._level (mirror): score classification only — a computable
    score keeps strong/weak even while provisional; only a missing
    score is neutral (level is never forced neutral by state)."""
    if value is None:
        return "neutral"
    return "strong" if value >= threshold else "weak"


def _pulse_support(count, required) -> dict:
    """matrix._support (mirror): evidence indicator (not a CI)."""
    req = max(1, int(required))
    ratio = min(1.0, int(count) / req)
    return {"count": int(count), "required": req, "ratio": round(ratio, 4)}


def _pulse_state(has_context, count, required) -> str:
    """matrix._state (mirror): empty / learning / provisional / ready."""
    if not has_context:
        return "empty"
    if not count:
        return "learning"
    return "ready" if count >= max(1, int(required)) else "provisional"


# Momentum knobs — MIRROR of pulse/momentum.py (the child has the plugin
# root off its sys.path, so it cannot import that module). Keep these
# constants + the formula byte-equivalent to pulse/momentum.py.
_PULSE_MOMENTUM_BAND = 0.02
_PULSE_MOMENTUM_DECIMALS = 4
# NOTE: value maps the RAW share-delta straight (0.5 + 0.5*surge) with no
# expanding-curve gain — mirrors pulse/momentum.py, so simultaneous surgers
# spread by real magnitude instead of all saturating at 1.0.


def _pulse_momentum(recent_counts: dict, cumulative_counts: dict) -> dict:
    """pulse.momentum.surge_values (mirror): per-tool rich-get-richer surge
    from recent vs cumulative activity shares. Pure + deterministic.

    A tool hotter than its long-run baseline is 'surging' (value > 0.5,
    ranked to the front); cooler is 'cooling' (value < 0.5)."""
    tools = sorted(set(recent_counts) | set(cumulative_counts))

    def _shares(counts, total):
        if not total:
            return {k: 0.0 for k in counts}
        return {k: counts.get(k, 0) / total for k in counts}

    recent_total = sum(max(0, v) for v in recent_counts.values())
    cum_total = sum(max(0, v) for v in cumulative_counts.values())
    recent_share = _shares(recent_counts, recent_total)
    cum_share = _shares(cumulative_counts, cum_total)
    out: dict[str, dict] = {}
    for t in tools:
        s = recent_share.get(t, 0.0) - cum_share.get(t, 0.0)
        value = 0.5 + 0.5 * s
        state = "surging" if s >= _PULSE_MOMENTUM_BAND else \
            "cooling" if s <= -_PULSE_MOMENTUM_BAND else "steady"
        d = _PULSE_MOMENTUM_DECIMALS
        out[t] = {"momentum": round(s, d), "surge": round(s, d),
                  "value": round(min(1.0, max(0.0, value)), d), "state": state}
    return out


# Stage 3 lane ranking — MIRROR of pulse/matrix.py (pure projection,
# same hysteresis quantization + deterministic tie-breaks). Keep the two
# in sync; tests/test_pulse_lanes.py pins live == fallback lanes.
_PULSE_LANE_ORDER = ["live", "goal", "good", "friction"]
_PULSE_HYSTERESIS = 1e-6


def _pulse_q(score) -> int:
    if score is None:
        return 0
    return int(round(float(score) / _PULSE_HYSTERESIS))


def _pulse_sem_key(entry: dict):
    value = entry.get("value")
    support = entry.get("support")
    if isinstance(support, dict):
        ratio = support.get("ratio", 0.0)
    else:
        ratio = max(((entry.get(p) or {}).get("support") or {}).get("ratio", 0.0)
                    for p in ("user", "objective"))
    return (0 if value is not None else 1, -_pulse_q(value), -ratio,
            entry.get("tool_name", ""), entry.get("tool_id", -1))


def _pulse_live_key(entry: dict):
    mom = entry.get("momentum") or {}
    act = entry.get("activity") or {}
    has = bool(act.get("calls_started") or act.get("calls_completed")
               or act.get("errors") or act.get("last_seen") is not None)
    total = (int(act.get("calls_started") or 0)
             + int(act.get("calls_completed") or 0)
             + int(act.get("errors") or 0))
    last = act.get("last_seen")
    return (0 if has else 1, -_pulse_q(mom.get("value", 0.5)), total * -1,
            -(float(last) if isinstance(last, (int, float)) else -1.0),
            entry.get("tool_name", ""), entry.get("tool_id", -1))


def _pulse_assign_ranks(entries: list, key_fn) -> list:
    ordered = sorted(entries, key=key_fn)
    for index, entry in enumerate(ordered, start=1):
        entry["rank"] = index
    return ordered


def _pulse_lanes_from_rows(rows, thresholds=None) -> dict:
    """Mirror of pulse/matrix.py lanes_from_rows (legacy rows -> v2
    lanes). Used to attach the additive envelope to a live engine view
    that predates the lanes field."""
    thr = {**_PULSE_DEFAULT_THRESHOLDS, **(thresholds or {})}
    live, goal, good, friction = [], [], [], []
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        tool = row.get("tool_name") or row.get("tool") or ""
        tid = row.get("tool_id", row.get("id", -1))
        live_row = row.get("live") or {}
        mom = row.get("momentum") or {"momentum": 0.0, "surge": 0.0,
                                      "value": 0.5, "state": "steady"}
        live.append({
            "tool_id": tid, "tool_name": tool, "rank": 0,
            "activity": {
                "calls_started": int(live_row.get("calls_started") or 0),
                "calls_completed": int(live_row.get("calls_completed") or 0),
                "errors": int(live_row.get("errors") or 0),
                "first_seen": live_row.get("first_seen"),
                "last_seen": live_row.get("last_seen"),
                "recent_capsules": 0,
            },
            "momentum": {"momentum": mom.get("momentum", 0.0),
                         "surge": mom.get("surge", mom.get("momentum", 0.0)),
                         "value": mom.get("value", 0.5),
                         "state": mom.get("state", "steady")},
        })
        for lane, col, threshold, is_goal in (
                (goal, "goal", thr["goal"], True),
                (good, "good", thr["good"], False)):
            cell = row.get(col) or {}
            support = cell.get("support") or _pulse_support(
                int(cell.get("support_count") or 0),
                int(cell.get("readiness_threshold") or 3))
            entry = {
                "tool_id": tid, "tool_name": tool, "rank": 0,
                "value": cell.get("value"), "threshold": threshold,
                "level": cell.get("level") or _pulse_level(cell.get("value"),
                                                           threshold),
                "state": cell.get("state") or "learning",
                "support": support,
            }
            if is_goal:
                entry["evidence"] = {
                    "source": ("prototype_banks" if support["count"] else "none"),
                    "prototype_count": support["count"], "capsule_count": 0}
            else:
                entry["evidence_source"] = ("good_prototypes"
                                            if support["count"] else "none")
            lane.append(entry)
        fcell = row.get("friction") or {}
        fthr = thr["friction"]
        user_support = fcell.get("user_support") or _pulse_support(0, 3)
        obj_support = fcell.get("objective_support") or _pulse_support(0, 3)
        friction.append({
            "tool_id": tid, "tool_name": tool, "rank": 0,
            "value": fcell.get("value"), "threshold": fthr,
            "level": fcell.get("level") or _pulse_level(fcell.get("value"), fthr),
            "state": fcell.get("state") or "learning",
            "dominant_source": fcell.get("dominant_source"),
            "user": {"value": fcell.get("user"),
                     "level": _pulse_level(fcell.get("user"), fthr),
                     "state": fcell.get("user_state") or "learning",
                     "support": user_support},
            "objective": {"value": fcell.get("objective"),
                          "level": _pulse_level(fcell.get("objective"), fthr),
                          "state": fcell.get("objective_state") or "learning",
                          "support": obj_support},
        })
    return {
        "live": _pulse_assign_ranks(live, _pulse_live_key),
        "goal": _pulse_assign_ranks(goal, _pulse_sem_key),
        "good": _pulse_assign_ranks(good, _pulse_sem_key),
        "friction": _pulse_assign_ranks(friction, _pulse_sem_key),
    }


def _pulse_v2_envelope(rows, lanes, available, columns) -> dict:
    return {
        "schema_version": 2,
        "available": bool(available),
        "lane_order": list(_PULSE_LANE_ORDER),
        "lanes": lanes if isinstance(lanes, dict) else {
            "live": [], "goal": [], "good": [], "friction": []},
        "rows": list(rows or []),
        "columns": list(columns or _PULSE_COLUMNS),
    }


def _unpack_pulse_vector(blob) -> list[float] | None:
    """float32 LE BLOB -> list[float] (the sketch convention shared with
    events.sketch and the pulse writer's pack_vector); malformed -> None."""
    if not blob:
        return None
    if len(blob) % 4:
        return None
    return list(struct.unpack(f"<{len(blob) // 4}f", bytes(blob)))


def _pulse_shape() -> dict:
    """Profile-DB fallback for the isolated dashboard half (4-column
    matrix rows x 4).

    The plugin-host child cannot import the agent-side in-memory engine,
    so PULSE state is bridged through the profile DB the agent half
    persists (storage.migration v4): ``pulse_tools`` (LIVE rows) plus the
    bounded derived-vector state (``pulse_prototypes`` banks, the derived
    ``pulse_goal`` vector, recent ``pulse_capsules`` vectors). Empty
    banks stay neutral and never hide tool rows; the response keeps the
    live engine's {rows, columns, available} contract. DB unavailable /
    read fault -> documented empty shape, never a 500.
    """
    shape = {"schema_version": 2,
             "lane_order": list(_PULSE_LANE_ORDER),
             "lanes": {"live": [], "goal": [], "good": [], "friction": []},
             "rows": [], "columns": list(_PULSE_COLUMNS),
             "available": False}
    try:
        conn = _connect()
    except Exception:
        return shape
    tool_rows, banks, goal_vector, capsules = [], {}, None, {}
    try:
        _migration.schema_bootstrap(conn)
        tool_rows = conn.execute(
            "SELECT id, tool_name, first_seen, last_seen,"
            " calls_started, calls_completed, errors"
            " FROM pulse_tools ORDER BY first_seen, id").fetchall()
        if not tool_rows:
            return shape
        bank_meta: dict = {}
        for tool, bank, blob, backend, model_version in conn.execute(
                "SELECT tool_name, bank, vector, backend, model_version"
                " FROM pulse_prototypes"
                " ORDER BY tool_name, bank, id"):
            if bank not in ("good", "friction", "objective_friction"):
                continue
            vec = _unpack_pulse_vector(blob)
            if vec is None:
                continue
            banks.setdefault(tool, {"good": [], "friction": [],
                                    "objective_friction": []}) \
                .setdefault(bank, []).append(vec)
            # v8: the writer delete+inserts a tool's whole bank
            # under one encoder, so the newest row's tags cover
            # the bank.
            bank_meta.setdefault(tool, {})[bank] = {
                "backend": backend,
                "model_version": model_version}
        goal_row = conn.execute(
            "SELECT ts, vector, backend FROM pulse_goal WHERE id = 1"
        ).fetchone()
        if goal_row is not None:
            goal_vector = _unpack_pulse_vector(goal_row[1])
        for tool, ts, blob, backend, model_version in conn.execute(
                "SELECT tool_name, ts, vector, backend, model_version"
                " FROM pulse_capsules"
                " ORDER BY tool_name, ts, id"):
            vec = _unpack_pulse_vector(blob)
            if vec is not None:
                capsules.setdefault(tool, []).append(
                    {"vector": vec, "ts": ts, "tool_name": str(tool),
                     "backend": backend,
                     "model_version": model_version})
    except Exception:
        return shape
    finally:
        conn.close()

    cur = _settings_current()
    thresholds = {}
    for key in ("goal", "good", "friction"):
        value = cur.get(f"pulse.thresholds.{key}")
        thresholds[key] = (value if value is not None
                           else _PULSE_DEFAULT_THRESHOLDS[key])
    min_labeled = cur.get("pulse.min_labeled")
    min_labeled = int(min_labeled) if min_labeled is not None \
        else _PULSE_DEFAULT_MIN_LABELED
    # Per-category readiness minimums: each falls back to the legacy
    # pulse.min_labeled when unset (core.settings.readiness_thresholds).
    def _required(key):
        value = cur.get(key)
        return int(value) if value is not None else min_labeled
    goal_req = _required("pulse.goal_min_references")
    good_req = _required("pulse.good_min_samples")
    user_fric_req = _required("pulse.user_friction_min_samples")
    obj_fric_req = _required("pulse.objective_friction_min_samples")

    rows = []
    lanes_live, lanes_goal, lanes_good, lanes_fric = [], [], [], []
    # v0.1.10 (review fix 2): ONE authoritative computation path — the
    # fallback feeds the profile-DB state through pulse.matrix.build_
    # matrix / build_lanes (the exact scoring, support and ranking the
    # live engine uses) instead of re-deriving it inline. The legacy row
    # projection and the v2 lanes envelope stay byte-compatible; the
    # rows->lanes mirror (_pulse_lanes_from_rows) remains a
    # compatibility path for pre-v0.1.10 engine responses only.
    try:
        from pulse import matrix as _pulse_matrix
        from pulse import prototypes as _pulse_protos
        from pulse import velocity as _pulse_velocity
    except ModuleNotFoundError:
        # Isolated plugin-host child: its startup cwd is NOT the plugin
        # root, so the bare top-level names are off its sys.path (the
        # 502 "No module named 'pulse'" on /pulse). Bootstrap from THIS
        # file's location (dashboard/ -> plugin root) and retry once.
        # The gateway half already imports fine — this path never runs
        # there.
        _root = str(Path(__file__).resolve().parent.parent)
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from pulse import matrix as _pulse_matrix
        from pulse import prototypes as _pulse_protos
        from pulse import velocity as _pulse_velocity

    class _DbRegistry:
        """pulse_tools rows as the matrix's registry projection."""

        def __init__(self, rows_):
            self._rows = rows_

        def list_tools(self):
            return list(self._rows)

    registry_rows = [{"tool_name": tr[1], "id": tr[0],
                      "calls_started": int(tr[4]),
                      "calls_completed": int(tr[5]),
                      "errors": int(tr[6]),
                      "first_seen": tr[2], "last_seen": tr[3]}
                     for tr in tool_rows]
    store = _pulse_protos.PrototypeStore(
        cap=64, min_labeled=min_labeled,
        goal_min_references=goal_req,
        good_min_samples=good_req,
        user_friction_min_samples=user_fric_req,
        objective_friction_min_samples=obj_fric_req)
    for tool, bank_labels in banks.items():
        for blabel, vecs in bank_labels.items():
            meta = bank_meta.get(tool, {}).get(blabel) or {}
            provenance = None
            if meta.get("backend") is not None or \
                    meta.get("model_version") is not None:
                provenance = (meta.get("backend"),
                              meta.get("model_version"))
            for vec in vecs:
                store.add(tool, vec, blabel, provenance=provenance)
        store.mark_clean(str(tool))
    # Active embedding-space identity for the compatibility gate: the
    # settings encoder is the active space; the persisted goal row's
    # backend is checked against it (a goal from another space is
    # gated out by the matrix, never scored into the pool). The
    # isolated child creates the backend locally when it can (the
    # tier-0 sketch is pure python) to report its model_version.
    active_encoder = cur.get("perception.encoder")
    model_version = None
    try:
        from perception import encoder as _active_encoder
        _active_backend = _active_encoder.create_backend(
            None, str(active_encoder) if active_encoder else None)
        model_version = getattr(_active_backend, "model_version", None)
    except Exception:
        pass
    vector_space = {"id": str(active_encoder) if active_encoder else None,
                    "model_version": model_version,
                    "dim": len(goal_vector) if goal_vector else None}
    goal_backend = goal_row[2] if goal_row is not None else None
    # C.1: per-tool event-rate velocity (persisted tool_start throughput
    # in a rolling window) — the "fast-decay" LIVE signal, computed for
    # free from the events table (idx_events_ts). Fault-tolerant -> {}
    # so a read fault degrades to the legacy capped-capsule sort, never
    # a 500.
    event_rates: dict = {}
    try:
        vconn = _connect()
        _migration.schema_bootstrap(vconn)
        event_rates = _pulse_velocity.event_velocity(
            vconn, window_s=_pulse_velocity.PULSE_VELOCITY_WINDOW_S)
        vconn.close()
    except Exception:
        event_rates = {}
    common = dict(
        registry=_DbRegistry(registry_rows), prototypes=store,
        session_state=None,
        goal_vector=goal_vector, goal_text=None, backend=None,
        capsules_by_tool=capsules,
        thresholds=thresholds,
        vector_space=vector_space, goal_backend=goal_backend,
        event_rates=event_rates)
    rows = _pulse_matrix.build_matrix(**common)
    lanes = _pulse_matrix.build_lanes(**common)
    shape["lanes"] = lanes
    shape["rows"] = rows
    shape["available"] = True
    return shape


def _pulse_live_view() -> dict | None:
    """The in-process engine view (gateway agent half) when it holds
    rows; None when the engine is unavailable (isolated child) or empty —
    the caller then serves the profile-DB fallback so persisted tool
    rows are never hidden by an empty in-memory engine."""
    try:
        eng = importlib.import_module("engine")
    except Exception:
        return None
    try:
        view = eng.get_engine().pulse_view()
    except Exception:
        return None
    if not isinstance(view, dict) or not view.get("rows"):
        return None
    return view


def _empty_perception(dims) -> dict:
    """Documented empty perception shape: no patterns, per-dim signals
    present but None (never invented), ``perception_available`` False.
    Never a 500; used when the engine lacks the key or the DB has no
    persisted derived row."""
    return {
        "patterns": [],
        "dim_signals": {d: {"prompt": None, "agent": None} for d in dims},
        "perception_available": False,
    }


# Live-expression / morphology neutral literals (module constants so the
# fault paths never depend on a live import): identical shapes to
# perception.patterns.neutral_live_expression() / neutral_morphology()
# and engine.NEUTRAL_*.
_NEUTRAL_LIVE_EXPRESSION: dict = {
    "openness": 0.0, "focus": 0.0, "resonance": 0.5, "tension": 0.5,
    "intensity": 0.0, "confidence": 0.0, "stage": "none",
    "updated_at": None,
}
_NEUTRAL_MORPHOLOGY: dict = {
    "pair": None, "strength": 0.0, "confidence": 0.0, "evidence": 0,
    "available": False, "seed": False, "centers": 0,
    "line_density": 0.0, "contour_asymmetry": 0.0,
    "echo_strength": 0.0, "gesture_bias": 0.0,
    # reference-seed structure offsets (the "expand the algo" per-set seed;
    # all zero / seed False here -> the neutral pencil face)
    "faceWidth": 0.0, "headWidth": 0.0, "jaw": 0.0, "brow": 0.0,
    "eyeAspect": 0.0, "hairMass": 0.0, "shoulder": 0.0,
}


def _morphology_from_history(conn) -> dict:
    """Reconstruct the learned-pair morphology from the persisted
    ``pattern_affinity_history`` (bounded top-three rows, shared
    turn_key). This is the restart/isolated-dashboard path (plan §5.4):
    the mutation is derived from already-persisted history, so no new
    storage. Any fault or missing import returns the neutral shape."""
    try:
        from perception import patterns as _pats
    except Exception:
        try:
            from ..perception import patterns as _pats  # type: ignore
        except Exception:
            return dict(_NEUTRAL_MORPHOLOGY)
    try:
        rows = conn.execute(
            "SELECT ts, turn_key, top_pattern FROM "
            "pattern_affinity_history WHERE top_pattern IS NOT NULL "
            "ORDER BY ts DESC, id DESC").fetchall()
    except Exception:
        return dict(_NEUTRAL_MORPHOLOGY)
    if not rows:
        return dict(_NEUTRAL_MORPHOLOGY)
    try:
        morpho = _pats.morphology_from_rows(rows)
    except Exception:
        return dict(_NEUTRAL_MORPHOLOGY)
    return morpho if isinstance(morpho, dict) else dict(_NEUTRAL_MORPHOLOGY)


def _reference_seed_morphology() -> dict:
    """The per-set reference-seed structure morphology (or neutral). Reads
    ``meta.reference_state`` (the rolling per-dim L2 windows) and projects it
    to a bounded, deterministic face-structure signature. Any fault -> the
    neutral morphology shape (never a 500). This is the 'expand the algo'
    seed: every session's face differs without a demographic / likeness.
    """
    try:
        from perception import patterns as _pats
    except Exception:
        try:
            from ..perception import patterns as _pats  # type: ignore
        except Exception:
            return dict(_NEUTRAL_MORPHOLOGY)
    try:
        conn = _connect()
    except Exception:
        return dict(_NEUTRAL_MORPHOLOGY)
    reference_state = None
    try:
        _migration.schema_bootstrap(conn)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='reference_state'").fetchone()
        if row and row[0]:
            import json
            reference_state = json.loads(row[0])
    except Exception:
        reference_state = None
    finally:
        conn.close()
    try:
        return _pats.structure_morphology(reference_state, _dimensions())
    except Exception:
        return dict(_NEUTRAL_MORPHOLOGY)


def _merge_morphology_with_seed(current_morphology, dims) -> dict:
    """Compose the learned-pair treatment (``current_morphology``) with the
    reference-seed structure. Reversible: an empty reference -> neutral seed
    -> the current morphology passes through unchanged (today's look)."""
    try:
        from perception import patterns as _pats
    except Exception:
        try:
            from ..perception import patterns as _pats  # type: ignore
        except Exception:
            return dict(current_morphology or _NEUTRAL_MORPHOLOGY)
    seed = _reference_seed_morphology()
    return _pats.merge_morphology(
        current_morphology or dict(_NEUTRAL_MORPHOLOGY), seed)


def _portrait_db_fallback(shape: dict, dims) -> dict:
    """Profile-DB fallback for the isolated dashboard half: bridges the
    persisted observed track through ``portrait_history`` (v3; the same
    resolver the /drift fallback uses) and the DERIVED perception numbers
    through ``pattern_affinity_history`` (v5). Fills
    ``dimensions`` and ``user`` only — the confidence and interpreted
    (agent) per-dim tracks are not persisted, so they stay None, never
    invented; perception per-dim signals likewise stay None (no per-dim
    columns are persisted) while ``patterns`` carries the LATEST ROW PER
    top_pattern (E1 reader-side completeness: after a restart every
    pattern that ever won a turn surfaces from its own most recent row,
    deterministic per-name ts desc with id desc tie-break; E3/v7: the
    engine now persists up to three rows per turn — rank 1 winner plus
    runner-ups — so per-name entries take that name's latest row at any
    rank, the strongest-card entry is the latest RANK-1 row, and the
    repeated-window counts rank-1 rows only; pre-v7 rows default to
    rank 1, so all-rank-1 data behaves exactly as before). Empty table /
    no table / DB fault ->
    the documented empty shape, never a 500."""
    if "perception" not in shape:
        shape["perception"] = _empty_perception(dims)
    try:
        conn = _connect()
    except Exception:
        return shape
    try:
        _migration.schema_bootstrap(conn, _DDL)
        # --- perception: LATEST ROW PER top_pattern (E1 reader-side
        # completeness; the table is derived-persistence only — D4).
        # E3 (v7): the engine persists up to THREE rows per turn (rank
        # 1 = the winner, ranks 2/3 = runner-ups carrying that
        # pattern's own derived values). Two selection refinements
        # follow from the shared-ts triples:
        #   * CARD = the latest RANK-1 row (id-desc alone would
        #     surface a runner-up as "overall latest");
        #   * REPEATED-WINDOW counts rank-1 rows only (repeated =
        #     WON the top).
        # Per-name entries still take that name's latest row at ANY
        # rank, so runner-up evidence now surfaces for its name.
        # Pre-v7 rows default to rank 1: all-rank-1 data behaves
        # byte-identically to the pre-v7 (E1) semantics.
        try:
            prow = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='pattern_affinity_history'").fetchone()
            if prow is not None:
                rows = conn.execute(
                    "SELECT top_pattern, rank, top_prompt_affinity, "
                    "top_agent_affinity, top_margin, top_amplification, "
                    "evidence, confidence FROM pattern_affinity_history "
                    "WHERE top_pattern IS NOT NULL "
                    "ORDER BY ts DESC, id DESC").fetchall()
                if rows:
                    # Latest row PER name (first occurrence in ts-desc
                    # order, any rank). Column layout:
                    # (name, rank, prompt_aff, agent_aff, margin, amp,
                    #  evidence, confidence).
                    rank_of = lambda r: (r[1] or 1)
                    latest_idx: dict = {}
                    for i, r in enumerate(rows):
                        latest_idx.setdefault(r[0], i)
                    # CARD: the latest rank-1 row (row index). Hand-
                    # crafted or exotic data with no rank-1 row falls
                    # back to the E1 ordering (ts-desc), never a 500.
                    card_i = next((i for i, r in enumerate(rows)
                                   if rank_of(r) == 1), None)
                    if card_i is None:
                        card_i = 0
                    order = [rows[card_i][0]]
                    order += [n for n in sorted(latest_idx,
                                                 key=lambda n: latest_idx[n])
                              if n != rows[card_i][0]]
                    patterns = []
                    for name in order:
                        i = card_i if name == rows[card_i][0] \
                            else latest_idx[name]
                        r = rows[i]
                        # Repeated window: up to 3 rank-1 rows
                        # immediately before this row's position;
                        # repeated = that name also WON within it.
                        prior = [p for p in rows[i + 1:i + 4]
                                 if rank_of(p) == 1]
                        patterns.append({
                            "name": name,
                            "prompt_affinity": r[2],
                            "agent_affinity": r[3],
                            "margin": r[4],
                            "amplification": r[5],
                            "evidence": int(r[6]) if r[6] is not None else 0,
                            "confidence": r[7],
                            "repeated": any(p[0] == name for p in prior),
                        })
                    shape["perception"] = {
                        "patterns": patterns,
                        "dim_signals": {
                            d: {"prompt": None, "agent": None}
                            for d in dims},
                        "perception_available": True,
                    }
        except Exception:
            shape["perception"] = _empty_perception(dims)
        # Morphology (plan §5.4): the learned-pair mutation survives
        # process restarts because it is derived from the persisted
        # top-three pattern history — reconstructed lazily here, in the
        # isolated dashboard child that has no live engine state.
        try:
            shape["morphology"] = _morphology_from_history(conn)
        except Exception:
            shape["morphology"] = dict(_NEUTRAL_MORPHOLOGY)
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
        latest = conn.execute(
            f"SELECT {', '.join(present)} FROM portrait_history "
            "ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
        if latest is None:
            return shape
        for i, d in enumerate(present):
            v = latest[i]
            if v is not None:
                shape["dimensions"][d] = v
                shape["user"][d] = v
        # Availability gate mirrors the live route: at least one
        # observed value (an all-NULL row carries no data).
        shape["available"] = any(v is not None for v in
                                 shape["dimensions"].values())
    except Exception:
        return {"available": False,
                "dimensions": {d: None for d in dims},
                "confidence": {d: None for d in dims},
                "user": {d: None for d in dims},
                "agent": {d: None for d in dims},
                "perception": _empty_perception(dims),
                "live_expression": dict(_NEUTRAL_LIVE_EXPRESSION),
                "morphology": dict(_NEUTRAL_MORPHOLOGY)}
    finally:
        conn.close()
    return shape


def _portrait_shape() -> dict:
    """Six dimension values + per-dim confidence + user/agent split
    (S3 perception.portrait when importable; else the profile-DB
    fallback over portrait_history for the isolated dashboard half).

    The fallback bridges the OBSERVED track only: portrait_history
    stores the six persisted observed dimension values (v3). The
    confidence and interpreted (agent) tracks are not persisted, so
    they stay None — never invented. Persisted observations are never
    hidden by an empty in-memory state: whenever the in-process probe
    yields no observed values — probe unimportable (isolated child) or
    empty (fresh engine after a restart) — the DB fallback is
    consulted. Empty table / all-NULL latest row -> the documented
    empty shape; DB faults -> empty shape, never a 500.
    """
    dims = _dimensions()
    shape = {
        "available": False,
        "dimensions": {d: None for d in dims},
        "confidence": {d: None for d in dims},
        "user": {d: None for d in dims},
        "agent": {d: None for d in dims},
        "perception": _empty_perception(dims),
        # Transient live state is NEVER reconstructed from persisted
        # history (plan §3.4): the fallback serves the neutral resting
        # expression. The morphology IS reconstructed from the
        # persisted pattern history below (plan §5.4).
        "live_expression": dict(_NEUTRAL_LIVE_EXPRESSION),
        "morphology": dict(_NEUTRAL_MORPHOLOGY),
    }
    _, fn = _engine_first(
        ("perception.portrait", "perception.portrait_store"),
        ("portrait", "get_portrait", "build_portrait", "compute_portrait",
         "current_portrait", "load_portrait", "portrait_for_text"))
    if fn is not None:
        try:
            result = fn()
        except TypeError:
            result = None  # needs arguments (e.g. text) — nothing in-memory
        except Exception:
            result = None
        if isinstance(result, dict):
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
                # LIVE perception pass-through (additive key): when the
                # probe carries 'perception', pass it through unchanged;
                # when absent, keep the documented empty shape.
                if isinstance(result.get("perception"), dict):
                    shape["perception"] = result["perception"]
            except Exception:
                pass  # probe filled nothing: consult the persisted state
    # In-process probe absent or empty -> bridge from the profile DB
    # so persisted observations survive restarts and process boundaries.
    if not shape["available"]:
        shape = _portrait_db_fallback(shape, dims)
    # Per-set reference-seed structure ("expand the algo"): make every
    # session's face differ via a bounded, deterministic projection of its
    # rolling reference — additive on the learned-pair treatment, reversible
    # (empty reference -> neutral seed -> today's look).
    shape["morphology"] = _merge_morphology_with_seed(
        shape.get("morphology"), dims)
    return shape


def _drift_shape() -> dict:
    """Per-dim time-series sparkline arrays from stored portrait history.
    Empty arrays until portrait_history holds rows. (v3: the table is
    always created by the schema bootstrap, so availability means AT
    LEAST ONE ROW — matching the engine drift_view contract; an empty
    table is "no data", not an available empty series.)"""
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
            # Available only with at least one row (empty table = no data,
            # mirroring engine.drift_view's bool(_history)).
            shape["available"] = bool(rows)
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
    + the PULSE domain). Never touches config. Idempotent; returns
    counts. The v4 schema bootstrap (first line) guarantees the pulse
    tables exist before the deletes."""
    counts = {"events": 0, "portrait_history": 0,
              "pulse_tools": 0, "pulse_prototypes": 0,
              "pulse_goal": 0, "pulse_capsules": 0}
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
        # PULSE domain (v4): the in-memory engine state is wiped by the
        # route's reset_engine() call; these tables would otherwise
        # resurrect the wiped view on the next isolated /pulse read.
        for table in ("pulse_tools", "pulse_prototypes", "pulse_goal",
                      "pulse_capsules"):
            cur = conn.execute(f"DELETE FROM {table}")
            counts[table] = cur.rowcount if cur.rowcount is not None else 0
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
async def pulse(after_event_id: int | None = Query(default=None),
                tool_name: str | None = Query(default=None)):
    """Pulse matrix (rows x 4 columns) + additive v2 lanes envelope:
    the live in-process engine view when the agent half holds rows;
    otherwise the profile-DB fallback (isolated dashboard child).
    Rows/columns are byte-compatible legacy projections; `lanes` is
    authoritative for new consumers. Empty banks stay neutral and
    persisted tool rows are never hidden by an empty engine. Never 500s.

    v0.1.10 (spec §3.3): additive `activity_events` — a cursor-based
    page of persisted tool_start event metadata (no content). No
    cursor -> baseline high-water mark, no history; cursor -> new
    events after it. `rows`, `lanes`, `lane_order`, `schema_version`
    are untouched.

    C/B (additive): optional `tool_name` restricts the `activity_events`
    page to ONE tool's backlog (the pane's FILO flush-on-visibility reads
    a tool's unconsumed events from the persisted events table instead of
    hoarding them in renderer memory). Same cursor semantics; no change
    to the lanes/rows envelope."""
    view = _pulse_live_view()
    if view is not None:
        if "lanes" not in view or view.get("schema_version") != 2:
            thresholds = {}
            try:
                cur = _settings_current()
                for key in ("goal", "good", "friction"):
                    value = cur.get(f"pulse.thresholds.{key}")
                    if value is not None:
                        thresholds[key] = value
            except Exception:
                thresholds = {}
            view = {
                **view,
                "schema_version": 2,
                "lane_order": list(_PULSE_LANE_ORDER),
                "lanes": _pulse_lanes_from_rows(view.get("rows"),
                                                thresholds or None),
            }
        result = view
    else:
        result = _pulse_shape()
    result["activity_events"] = _pulse_activity_events(after_event_id,
                                                       tool_name=tool_name)
    return result


def _portrait_live_view() -> dict | None:
    """The in-process engine portrait view (gateway agent half) when it
    holds observed values; None when the engine is unavailable
    (isolated child) or empty — the caller then serves the profile-DB
    fallback so persisted observations are never hidden by an empty
    in-memory engine."""
    try:
        eng = importlib.import_module("engine")
    except Exception:
        return None
    try:
        view = eng.get_engine().portrait_view()
    except Exception:
        return None
    if not isinstance(view, dict):
        return None
    if not any(v is not None for v in (view.get("dimensions") or {}).values()):
        return None
    return view


@router.get("/portrait")
async def portrait():
    """Portrait: six dimensions + confidence + user/agent split +
    additive ``perception`` + optional ``live_expression`` (transient,
    neutral in the DB fallback) and ``morphology`` (learned-pair
    mutation, reconstructed from persisted history in the fallback).
    Live in-process engine data when it holds observed values; otherwise
    the profile-DB fallback (isolated dashboard child). Never 500s."""
    view = _portrait_live_view()
    if view is not None:
        if "perception" not in view:
            # Older engine code (or engine without the additive key):
            # documented empty perception, never 500, never invented.
            view = {**view, "perception": _empty_perception(_dimensions())}
        if "live_expression" not in view:
            view = {**view, "live_expression": dict(_NEUTRAL_LIVE_EXPRESSION)}
        if "morphology" not in view:
            view = {**view, "morphology": dict(_NEUTRAL_MORPHOLOGY)}
        return view
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


@router.get("/engine-status")
async def engine_status():
    """Read-only Tier-1 readiness for the dashboard encoder switcher.

    Reports the persisted ``perception.encoder`` and whether the Tier-1 ONNX
    model artifacts are installed locally (a hash-gated ``check_model`` probe,
    no worker spawn). Never a 500: any import/probe fault degrades to
    ``onnx_model: 'unavailable'`` with a one-line detail, so a missing optional
    path can't take the whole dashboard down."""
    cur = _settings_current()
    out = {
        "encoder": str(cur.get("perception.encoder", "tier-0")),
        "onnx_model": "unknown",
        "onnx_detail": None,
    }
    try:
        try:
            from ..perception import worker_client as _wc
        except Exception:  # isolated child / top-level repo
            from perception import worker_client as _wc  # type: ignore
        status, errors = _wc.check_model()
        out["onnx_model"] = status
        out["onnx_detail"] = "; ".join(errors) if errors else None
    except Exception as exc:
        out["onnx_model"] = "unavailable"
        out["onnx_detail"] = str(exc)
    return out


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
