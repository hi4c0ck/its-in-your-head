"""Durable user-data store (the plugin's OWN settings / user-data document).

User settings — adopted presets, engine configuration, any user-owned state —
persist here in the plugin profile DB (the ``meta`` KV store, key
``plugin_settings``), NOT in the gateway's transient ctx config and NOT in
pane/child in-memory state. Both halves of the plugin (the gateway agent half
and the isolated dashboard child) read and write the SAME store, so user data
survives every restart and stays consistent across processes.

Design (owner decision 2026-11-12):
- The profile DB is the SOURCE OF TRUTH for user settings. The ctx config API
  (``ctx.get_config`` / ``ctx.set_config``) is NO LONGER authoritative — at
  most a legacy mirror for consumers not yet migrated. A restart can never
  flush what is written here.
- This is a GENERIC user-data document (a flat key->value JSON object). C4
  engine keys are one kind of entry; an adopted preset / any user blob is
  another. Nothing here is C4-specific (C4 is left untouched).
- Fault-tolerant by invariant: a corrupt/absent document degrades to an empty
  store, never an exception, never a worker death. No raw content beyond the
  settings document is stored.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# The meta-KV key holding the durable user-data document (JSON object).
USER_SETTINGS_KEY = "plugin_settings"


def _bootstrap(conn) -> bool:
    """Ensure the ``meta`` KV table exists on ``conn`` (idempotent). False
    when the storage modules are unavailable (no degradation, no raise)."""
    try:
        try:
            from . import migration as _mig
        except ImportError:
            from storage import migration as _mig
        _mig.schema_bootstrap(conn)
        return True
    except Exception:
        logger.exception("its-in-your-head: userstore bootstrap failed")
        return False


def load(conn) -> dict:
    """The durable user-data document: a flat dict ({} when absent or
    corrupt). Never raises."""
    if conn is None:
        return {}
    if not _bootstrap(conn):
        return {}
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (USER_SETTINGS_KEY,)
        ).fetchone()
    except Exception:
        return {}
    if not row or not row[0]:
        return {}
    try:
        doc = json.loads(row[0])
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def save(conn, values: dict) -> bool:
    """Merge ``values`` into the durable document (read-modify-write) and
    commit. True when the write landed; False on any fault (the document is
    left as-is — a failed save never truncates user data)."""
    if conn is None or not isinstance(values, dict) or not values:
        return False
    if not _bootstrap(conn):
        return False
    doc = load(conn)
    doc.update(values)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (USER_SETTINGS_KEY, json.dumps(doc)))
        conn.commit()
        return True
    except Exception:
        logger.exception("its-in-your-head: user settings write failed")
        return False


def clear(conn, key: str) -> bool:
    """Remove ONE key from the durable document (an explicit user unset).
    True when the document was rewritten; False on any fault."""
    if conn is None or not isinstance(key, str) or not key:
        return False
    if not _bootstrap(conn):
        return False
    doc = load(conn)
    if key not in doc:
        return True  # already absent — the requested state holds
    doc.pop(key, None)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (USER_SETTINGS_KEY, json.dumps(doc)))
        conn.commit()
        return True
    except Exception:
        logger.exception("its-in-your-head: user settings clear failed")
        return False
