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


def _pulse_level(value, threshold, state) -> str:
    """matrix._level (mirror): a value never renders strong/weak for a
    'learning' tool or a missing value."""
    if value is None or state != "ready":
        return "neutral"
    return "strong" if value >= threshold else "weak"


# Momentum knobs — MIRROR of pulse/momentum.py (the child has the plugin
# root off its sys.path, so it cannot import that module). Keep these three
# constants + the formula byte-equivalent to pulse/momentum.py.
_PULSE_MOMENTUM_GAIN = 0.6
_PULSE_MOMENTUM_BAND = 0.02
_PULSE_MOMENTUM_DECIMALS = 4


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
    surge = {t: recent_share.get(t, 0.0) - cum_share.get(t, 0.0)
             for t in tools}
    peak = max((abs(surge[t]) for t in tools), default=0.0) or 1.0

    out: dict[str, dict] = {}
    for t in tools:
        s = surge[t]
        frac = abs(s) / peak
        momentum = (1.0 if s > 0.0 else -1.0) * (frac ** _PULSE_MOMENTUM_GAIN) \
            if s else 0.0
        value = min(1.0, max(0.0, 0.5 + 0.5 * momentum))
        state = "surging" if s >= _PULSE_MOMENTUM_BAND else \
            "cooling" if s <= -_PULSE_MOMENTUM_BAND else "steady"
        d = _PULSE_MOMENTUM_DECIMALS
        out[t] = {"momentum": round(momentum, d), "surge": round(s, d),
                  "value": round(value, d), "state": state}
    return out


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
    shape = {"rows": [], "columns": list(_PULSE_COLUMNS),
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
        for tool, bank, blob in conn.execute(
                "SELECT tool_name, bank, vector FROM pulse_prototypes"
                " ORDER BY tool_name, bank, id"):
            vec = _unpack_pulse_vector(blob)
            if vec is None:
                continue
            banks.setdefault(tool, {"good": [], "friction": [],
                                    "objective_friction": []}) \
                .setdefault(bank, []).append(vec)
        goal_row = conn.execute(
            "SELECT ts, vector, backend FROM pulse_goal WHERE id = 1"
        ).fetchone()
        if goal_row is not None:
            goal_vector = _unpack_pulse_vector(goal_row[1])
        for tool, ts, blob in conn.execute(
                "SELECT tool_name, ts, vector FROM pulse_capsules"
                " ORDER BY tool_name, ts, id"):
            vec = _unpack_pulse_vector(blob)
            if vec is not None:
                capsules.setdefault(tool, []).append({"vector": vec,
                                                      "ts": ts})
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

    rows = []
    # Momentum (Tier-0 positive feedback), mirrored inline: recent capsule
    # feed vs cumulative call count per tool. See _pulse_momentum.
    tool_names = [tr[1] for tr in tool_rows]
    recent_counts = {
        t: sum(1 for c in (capsules.get(t) or []) if c.get("vector") is not None)
        for t in tool_names
    }
    cum_counts = {tr[1]: int(tr[5]) for tr in tool_rows}
    momentum_map = _pulse_momentum(recent_counts, cum_counts)
    for (tool_id, tool, first_seen, last_seen, started, completed,
         errors) in tool_rows:
        bank = banks.get(tool, {"good": [], "friction": [],
                                "objective_friction": []})
        labeled = len(bank.get("good", ())) + len(bank.get("friction", ()))
        state = "ready" if labeled >= min_labeled else "learning"
        reference = (bank.get("good", []) + bank.get("friction", [])
                     + bank.get("objective_friction", []))
        if not reference:
            reference = [c["vector"] for c in
                         capsules.get(tool, [])[-_PULSE_CAPSULE_DISPLAY_CAP:]
                         if c.get("vector") is not None]
        goal_raw = _pulse_strongest(goal_vector, reference)
        goal_value = _pulse_floor(goal_raw)
        goal_cell = {"value": goal_value,
                     "state": "neutral" if goal_raw is None else state,
                     "level": _pulse_level(goal_value,
                                            thresholds["goal"], state)}

        good_raw = _pulse_strongest(goal_vector, bank.get("good", []))
        good_value = _pulse_floor(good_raw)
        good_cell = {"value": good_value,
                     "state": "neutral" if good_raw is None else state,
                     "level": _pulse_level(good_value,
                                            thresholds["good"], state)}

        friction_user = _pulse_strongest(goal_vector,
                                         bank.get("friction", []))
        friction_obj = _pulse_strongest(goal_vector,
                                        bank.get("objective_friction", []))
        friction_parts = [v for v in (friction_user, friction_obj)
                          if v is not None]
        friction_raw = max(friction_parts) if friction_parts else None
        friction_value = _pulse_floor(friction_raw)
        friction_cell = {
            "value": friction_value,
            "state": "neutral" if friction_raw is None else state,
            "level": _pulse_level(friction_value,
                                   thresholds["friction"], state),
            "user": _pulse_floor(friction_user),
            "objective": _pulse_floor(friction_obj),
        }

        rows.append({
            "tool_name": tool,
            "tool_id": tool_id,
            "state": state,
            "labeled": labeled,
            "live": {
                "calls_started": started,
                "calls_completed": completed,
                "errors": errors,
                "last_seen": last_seen,
                "first_seen": first_seen,
                # No in-process session state in the isolated child: the
                # global snapshot is reported empty, never guessed.
                "global": {},
            },
            "goal": goal_cell,
            "good": good_cell,
            "friction": friction_cell,
            "momentum": momentum_map.get(tool, {
                "momentum": 0.0, "surge": 0.0, "value": 0.5,
                "state": "steady"}),
        })
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
                "perception": _empty_perception(dims)}
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
async def pulse():
    """Pulse matrix (rows x 4 columns): the live in-process engine view
    when the agent half holds rows; otherwise the profile-DB fallback
    (isolated dashboard child). Empty banks stay neutral and persisted
    tool rows are never hidden by an empty engine. Never 500s."""
    view = _pulse_live_view()
    if view is not None:
        return view
    return _pulse_shape()


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
    additive ``perception``. Live in-process engine data when it holds
    observed values; otherwise the profile-DB fallback (isolated
    dashboard child). Never 500s."""
    view = _portrait_live_view()
    if view is not None:
        if "perception" not in view:
            # Older engine code (or engine without the additive key):
            # documented empty perception, never 500, never invented.
            view = {**view, "perception": _empty_perception(_dimensions())}
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
