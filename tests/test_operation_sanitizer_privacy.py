"""Sanitizer privacy regression — the REAL sanitize_operation() runs against
password / API-key / email / URL-credential / personal-name canaries, and the
resulting descriptors are verified through the production persistence path
(engine.on_events -> pulse_operations.operation).

Background: the old deny-token filter rejected `--password` but not the value
after it — `login --password hunter2` leaked `verb:hunter2` into a persistable
descriptor. Query terms and path components had the same broader problem.
Value extraction from command/query/pattern is now removed outright; paths
contribute only the allowlisted extension class; URLs only the categorical
kind:url role. A deny-token filter is NOT a privacy boundary.
"""
import sqlite3
import time

import pytest

import engine
import metrics
from core import events
from engine import Engine
from pulse import activity_encoder
from storage import database


# (id, tool, real args, forbidden fragments that must NEVER appear)
CANARIES = [
    ("password-flag-value", "terminal",
     {"command": "login --password hunter2"},
     ["hunter2", "password"]),
    ("password-attached", "terminal",
     {"command": "mysql --password=hunter2 -u root"},
     ["hunter2"]),
    ("password-short-flag", "terminal",
     {"command": "psql -U admin -p hunter2 dbname"},
     ["hunter2", "dbname"]),
    ("api-key-bearer", "terminal",
     {"command": "curl -H 'Authorization: Bearer sk-live-abc123XYZ' "
                 "https://api.example.com"},
     ["sk-live-abc123xyz", "bearer"]),
    ("api-key-code", "execute_code",
     {"code": "API_KEY='sk-proj-SECRETKEY123'\nprint(API_KEY)"},
     ["secretkey123", "sk-proj"]),
    ("api-key-query", "web_search",
     {"query": "sk-proj-SECRETKEY123 usage limits"},
     ["secretkey123", "sk-proj"]),
    ("email-query", "web_search",
     {"query": "john.doe@example.com salary"},
     ["john", "doe", "example.com", "salary"]),
    ("email-pattern", "search_files",
     {"pattern": "alice.smith@corp.io"},
     ["alice", "smith", "corp"]),
    ("email-url-path", "browser_exec",
     {"url": "https://example.com/users/john.doe@example.com/profile"},
     ["john", "doe", "example.com", "profile"]),
    ("url-credentials", "browser_exec",
     {"url": "https://alice:s3cretP4ss@internal.corp.example/admin?token=xyz"},
     ["alice", "s3cretp4ss", "internal", "corp", "admin", "xyz"]),
    ("personal-path", "read_file",
     {"path": "C:/Users/john_smith/Documents/alice_resume_2026.pdf"},
     ["john", "smith", "alice", "resume"]),
    ("personal-dir", "write_file",
     {"path": "/home/john_smith/private/notes.md", "content": "secret text"},
     ["john", "smith", "private", "notes"]),
    ("personal-query", "web_search",
     {"query": "John Smith bankruptcy records"},
     ["john", "smith", "bankruptcy"]),
    ("git-credential-url", "terminal",
     {"command": "git push https://user:glpat-XXXX@gitlab.com/repo.git"},
     ["glpat", "xxxx", "gitlab"]),
]


@pytest.mark.parametrize("case", CANARIES, ids=[c[0] for c in CANARIES])
def test_canary_never_reaches_descriptor(case):
    """The real sanitizer, real args: no canary fragment survives."""
    _, tool, args, forbidden = case
    descriptor = events.sanitize_operation(tool, args)
    low = descriptor.lower()
    for fragment in forbidden:
        assert fragment.lower() not in low, \
            f"{fragment!r} leaked into descriptor {descriptor!r}"
    assert len(descriptor) <= 160
    assert descriptor.startswith("action:")
    assert f"tool:{tool}" in descriptor


# --- positive contract: what the descriptor IS allowed to carry ----------

def test_path_contributes_extension_category_only():
    d = events.sanitize_operation(
        "read_file", {"path": "D:/work/project/pulse/goal_trajectory.py"})
    assert d == "action:read tool:read_file kind:code arg:path:str"


def test_unknown_extension_is_generic_file_category():
    d = events.sanitize_operation("read_file", {"path": "/tmp/archive.xyz"})
    assert "kind:file" in d
    assert "archive" not in d and "tmp" not in d


def test_url_contributes_role_category_only():
    d = events.sanitize_operation(
        "browser_exec", {"url": "https://github.com/hi4c0ck/its-in-your-head"})
    assert d == "action:browse tool:browser_exec kind:url arg:url:str"


def test_command_and_query_contribute_no_semantics():
    for tool, args in (
            ("terminal", {"command": "python -m pytest tests/test_x.py"}),
            ("web_search", {"query": "trajectory descriptor"}),
            ("search_files", {"pattern": "def _assessment"})):
        d = events.sanitize_operation(tool, args)
        assert not events.has_operation_semantics(d), d


def test_semantic_marker_gate():
    assert events.has_operation_semantics(
        "action:read tool:read_file kind:code arg:path:str")
    assert not events.has_operation_semantics(
        "action:execute tool:terminal arg:command:str")


def test_metrics_row_boundary_carries_no_descriptor_text():
    """The events table has no operation column: descriptor text (and any
    raw arg value) never rides the metrics persistence boundary."""
    ev = events.tool_start(
        tool_name="terminal", args_chars=30,
        operation=events.sanitize_operation(
            "terminal", {"command": "login --password hunter2"}))
    row = metrics._row_from_event(ev)
    assert "hunter2" not in str(row)
    assert "operation" not in row


# --- capsule path: single policy + hostile passthrough rejection ---------

def test_capsule_delegates_to_core_sanitizer():
    cap = activity_encoder.build_capsule(
        {"type": "tool_end", "tool_name": "terminal", "status": "ok",
         "duration_ms": 10},
        args={"command": "login --password hunter2"})
    expected = events.sanitize_operation(
        "terminal", {"command": "login --password hunter2"})
    assert cap["operation"] == expected
    assert "hunter2" not in cap["text"]


def test_capsule_rejects_free_text_operation_passthrough():
    cap = activity_encoder.build_capsule(
        {"type": "tool_end", "tool_name": "terminal", "status": "ok"},
        args={"operation": "hunter2 secret payload",
              "command": "login --password hunter2"})
    assert "hunter2" not in cap["operation"]
    assert "secret" not in cap["operation"]
    assert cap["operation"].startswith("action:")


def test_capsule_accepts_sanitized_operation_passthrough():
    d = events.sanitize_operation("read_file", {"path": "pulse/x.py"})
    cap = activity_encoder.build_capsule(
        {"type": "tool_end", "tool_name": "read_file", "status": "ok"},
        args={"operation": d})
    assert cap["operation"] == d


# --- persisted descriptors: the REAL production path ---------------------

@pytest.fixture
def env(tmp_path):
    db = tmp_path / "privacy.db"
    database.set_override(db)
    engine._engine = None
    eng = engine.get_engine()
    yield eng, db
    engine._engine = None
    database.set_override(None)


def test_persisted_descriptors_are_canary_free(env):
    """tool_start/tool_end -> engine trajectory -> pulse_operations: the
    persisted operation column equals the sanitized descriptor and carries
    no canary fragment (verified by reading the DB directly)."""
    eng, db = env
    now = time.time()
    starts, ends, expected = [], [], {}
    for i, (cid, tool, args, forbidden) in enumerate(CANARIES):
        descriptor = events.sanitize_operation(tool, args)
        call_id = f"call-{i}"
        expected[call_id] = (descriptor, forbidden)
        starts.append({"type": "tool_start", "tool_name": tool,
                       "tool_call_id": call_id, "operation": descriptor,
                       "ts": now + i * 0.01})
        ends.append({"type": "tool_end", "tool_name": tool,
                     "tool_call_id": call_id, "status": "ok",
                     "duration_ms": 5, "operation": descriptor,
                     "ts": now + i * 0.01 + 0.005})
    eng.on_events(starts)
    eng.on_events(ends)

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT call_id, operation FROM pulse_operations").fetchall()
    finally:
        conn.close()
    assert rows, "production path persisted no operations"
    by_call = {call_id: op for call_id, op in rows}
    for call_id, (descriptor, forbidden) in expected.items():
        persisted = by_call.get(call_id)
        assert persisted is not None and persisted == descriptor, \
            f"{call_id}: persisted {persisted!r} != sanitized {descriptor!r}"
        for fragment in forbidden:
            assert fragment.lower() not in persisted.lower(), \
                f"{fragment!r} leaked into PERSISTED descriptor {persisted!r}"
