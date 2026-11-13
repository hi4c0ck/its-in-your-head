"""Durable user-data store (storage/userstore.py): the plugin's OWN
settings / user-data document in the profile DB (the source of truth that
survives every restart). Covers round-trip, merge, clear, fault-tolerance,
and restart survival (close + reopen the connection)."""
import sqlite3

from storage import userstore


def _conn(tmp_path):
    # userstore bootstraps the schema itself (idempotent) on first use.
    return sqlite3.connect(tmp_path / "data.db")


def test_save_then_load_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    assert userstore.load(conn) == {}
    assert userstore.save(conn, {"pulse.prototype_cap": 32}) is True
    assert userstore.load(conn) == {"pulse.prototype_cap": 32}
    conn.close()


def test_save_merges_into_existing_document(tmp_path):
    conn = _conn(tmp_path)
    userstore.save(conn, {"a": 1})
    userstore.save(conn, {"b": 2})
    assert userstore.load(conn) == {"a": 1, "b": 2}
    conn.close()


def test_clear_removes_one_key_only(tmp_path):
    conn = _conn(tmp_path)
    userstore.save(conn, {"a": 1, "b": 2})
    assert userstore.clear(conn, "a") is True
    assert userstore.load(conn) == {"b": 2}
    # Clearing an absent key is a successful no-op (state already holds).
    assert userstore.clear(conn, "zzz") is True
    assert userstore.load(conn) == {"b": 2}
    conn.close()


def test_save_empty_values_is_noop(tmp_path):
    conn = _conn(tmp_path)
    userstore.save(conn, {"a": 1})
    assert userstore.save(conn, {}) is False  # nothing to merge
    assert userstore.load(conn) == {"a": 1}  # a failed/empty save truncates
    conn.close()


def test_corrupt_document_degrades_to_empty(tmp_path):
    conn = _conn(tmp_path)
    userstore.save(conn, {"seed": 1})  # bootstraps the meta table
    conn.execute("UPDATE meta SET value = ? WHERE key = ?",
                 ("this is not json", userstore.USER_SETTINGS_KEY))
    conn.commit()
    assert userstore.load(conn) == {}  # fault-tolerant: degrades, never raises
    conn.close()


def test_survives_reopen_simulated_restart(tmp_path):
    # The actual 'never flush on restart' guarantee: write, close the
    # connection (process exit), reopen (new process), read back.
    conn = _conn(tmp_path)
    userstore.save(conn, {"pulse.prototype_cap": 32,
                          "perception.encoder": "tier-1"})
    conn.close()
    conn2 = _conn(tmp_path)
    doc = userstore.load(conn2)
    assert doc["pulse.prototype_cap"] == 32
    assert doc["perception.encoder"] == "tier-1"
    conn2.close()
