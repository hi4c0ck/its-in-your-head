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


# --- forged-prefix canaries: the passthrough TRUST gate ------------------
# A prefix check alone is not a boundary: these strings carry approved
# prefixes (or legacy ones) with forged content and must NEVER be accepted
# as trusted descriptors — neither by the capsule passthrough nor by the
# engine's trajectory intake (which persists the operation column).

FORGED_DESCRIPTORS = [
    "arg:password:hunter2",                                # forged arg value
    "kind:alice_smith",                                    # forged kind
    "verb:hunter2",                                        # legacy prefix
    "term:john.doe@example.com",                           # legacy + charset
    "file:alice_resume_2026.pdf",                          # legacy basename
    "host:internal.corp.example",                          # legacy host
    "action:execute tool:terminal verb:hunter2",           # canonical head + legacy tail
    "action:execute tool:terminal arg:command:hunter2",    # forged type name
    "action:execute tool:terminal arg:password:str",       # deny-token key
    "action:execute tool:terminal kind:secret",            # kind outside closed set
    "action:destroy tool:terminal",                        # forged action class
    "action:read tool:terminal",                           # action/tool mismatch
    "action:execute tool:--password",                      # flag-like tool field
    "action:execute tool:",                                # empty tool field
    "action:execute tool:terminal arg:command:str extra",  # bare token
    "action:execute tool:terminal arg:command:str,arg:timeout:int",  # comma
    "action:execute  tool:terminal",                       # double space
    " action:execute tool:terminal",                       # leading space
    "action:execute tool:terminal kind:code kind:url",     # duplicate kind
    "action:execute tool:terminal arg:command:str kind:code",  # kind after arg
    "action:execute tool:terminal arg:command:str arg:command:int",  # dup key
    "action:execute tool:terminal " + " ".join(
        f"arg:k{i}:str" for i in range(7)),                # > 6 arg tags
    "action:execute tool:terminal " + "x" * 200,           # over the 160 cap
    "hunter2 secret payload",                              # plain free text
    "action:execute tool:terminal arg:command:Hunter2",    # type-name case
    "action:execute tool:terminal arg::str",               # empty arg key
    "action:execute tool:terminal arg:command:",           # empty arg type
]


@pytest.mark.parametrize("forged", FORGED_DESCRIPTORS)
def test_forged_descriptors_fail_canonical_parse(forged):
    assert not events.is_canonical_operation(forged), \
        f"forged descriptor accepted: {forged!r}"


@pytest.mark.parametrize("forged", FORGED_DESCRIPTORS)
def test_forged_passthrough_is_resanitized_not_accepted(forged):
    args = {"operation": forged, "command": "login --password hunter2"}
    op = activity_encoder.sanitize_operation("terminal", args=args)
    assert op != forged
    assert events.is_canonical_operation(op)
    assert op == events.sanitize_operation("terminal", args)
    assert "hunter2" not in op and "alice" not in op


@pytest.mark.parametrize("case", CANARIES, ids=[c[0] for c in CANARIES])
def test_real_sanitizer_output_is_canonical(case):
    """Producer/gate lockstep: everything the sanitizer emits must parse."""
    _, tool, args, _ = case
    assert events.is_canonical_operation(events.sanitize_operation(tool, args))


def test_producer_canonical_for_exotic_and_mixed_args():
    class Weird:
        pass

    # Exotic type names collapse to the bounded 'other' category.
    d = events.sanitize_operation("terminal", {"command": "x", "obj": Weird()})
    assert "arg:obj:other" in d and events.is_canonical_operation(d)
    # Mixed semantic keys: at most ONE kind token (first sorted key wins).
    d = events.sanitize_operation(
        "read_file", {"path": "a.py", "url": "https://x.example"})
    assert d.count("kind:") == 1 and events.is_canonical_operation(d)
    # Deny-token keys collapse to the same fallback -> no duplicate arg keys.
    d = events.sanitize_operation("terminal", {"password": "x", "secret": "y"})
    assert d.count("arg:arg:") <= 1 and events.is_canonical_operation(d)
    # JSON type battery.
    d = events.sanitize_operation("terminal", {
        "a": None, "b": True, "c": [1], "d": {"k": "v"}, "e": 1.5, "f": 3})
    assert events.is_canonical_operation(d)
    # Long keys: the 160-char budget drops WHOLE tokens, never mid-token.
    d = events.sanitize_operation("terminal", {
        f"very_long_argument_key_{i}": "value" for i in range(6)})
    assert len(d) <= 160 and events.is_canonical_operation(d)


def test_forged_operations_never_persist(env):
    """The trajectory intake persists the operation column DIRECTLY (it
    does not route through the capsule passthrough) — forged descriptors
    fed as events must be re-derived, never stored as-is."""
    eng, db = env
    now = time.time()
    starts, ends = [], []
    for i, forged in enumerate(FORGED_DESCRIPTORS):
        call_id = f"forged-{i}"
        starts.append({"type": "tool_start", "tool_name": "terminal",
                       "tool_call_id": call_id, "operation": forged,
                       "ts": now + i * 0.01})
        ends.append({"type": "tool_end", "tool_name": "terminal",
                     "tool_call_id": call_id, "status": "ok",
                     "duration_ms": 5, "operation": forged,
                     "ts": now + i * 0.01 + 0.005})
    eng.on_events(starts)
    eng.on_events(ends)

    expected = events.sanitize_operation("terminal", None)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT call_id, operation FROM pulse_operations"
            " WHERE call_id LIKE 'forged-%'").fetchall()
    finally:
        conn.close()
    assert len(rows) == len(FORGED_DESCRIPTORS)
    for call_id, persisted in rows:
        assert persisted == expected, \
            f"{call_id}: forged operation persisted as {persisted!r}"
        assert events.is_canonical_operation(persisted)
        for fragment in ("hunter2", "alice", "secret", "password"):
            assert fragment not in persisted.lower()
