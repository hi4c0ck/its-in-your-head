"""Privacy gate: operation descriptors must not carry free-form command/query text."""
import sqlite3

from core.events import has_operation_semantics, sanitize_operation


CANARIES = (
    "hunter2",
    "SECRET-TOKEN-XYZ",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "alice@example.com",
    "password",
)


def test_command_secret_value_is_not_persisted():
    op = sanitize_operation("terminal", {"command": "login --password hunter2"})
    assert "hunter2" not in op
    assert "verb:" not in op and "term:" not in op
    assert op.startswith("action:execute tool:terminal")


def test_query_and_pattern_are_structural_only():
    for key in ("query", "q", "pattern"):
        op = sanitize_operation("search_files", {key: "alice@example.com SECRET-TOKEN-XYZ"})
        assert "alice" not in op and "SECRET-TOKEN-XYZ" not in op
        assert "term:" not in op and "verb:" not in op
        assert "kind:query" in op
        assert f"arg:{key}:str" in op


def test_every_canary_is_dropped_from_command_and_query():
    for tool, key in (("terminal", "command"), ("search_files", "query")):
        for canary in CANARIES:
            op = sanitize_operation(tool, {key: "look up " + canary + " now"})
            assert canary.lower() not in op.lower(), (key, canary, op)


def test_allowlisted_command_yields_canonical_class_only():
    op = sanitize_operation("terminal", {
        "command": "python -m pytest tests/test_goal.py --token hunter2",
    })
    assert "cmd:test" in op
    assert "test_goal" not in op and "hunter2" not in op
    assert has_operation_semantics(op)


def test_unapproved_command_has_no_semantic_token():
    op = sanitize_operation("terminal", {"command": "cook sandwich with milk"})
    assert not has_operation_semantics(op)


def test_canaries_never_reach_persisted_operation(tmp_path):
    from pulse.persistence import persist_trajectory
    from storage import database, migration

    database.set_override(tmp_path / "data.db")
    try:
        conn = database.connect()
        migration.schema_bootstrap(conn)
        conn.close()
        canary = "alice@example.com"
        op = sanitize_operation("search_files", {"query": "find " + canary})
        persist_trajectory(None, [{
            "call_id": "privacy-1", "tool": "search_files", "operation": op,
            "ts": 1.0, "status": "ok", "category": "pending",
        }])
        conn = sqlite3.connect(tmp_path / "data.db")
        row = conn.execute(
            "SELECT operation FROM pulse_operations WHERE call_id='privacy-1'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert canary not in row[0]
        assert "verb:" not in row[0] and "term:" not in row[0]
    finally:
        database.set_override(None)
