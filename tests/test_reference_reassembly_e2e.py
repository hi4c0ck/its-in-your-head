"""L-OOD/L-MIGRATE event-driven smoke: feeding a real user turn through
on_events reassembles the rolling reference + sets ood_drift + persists it,
and a second engine rehydrates the persisted window (the restart path)."""
import storage.database as _db


# A known prompt vector aligned with the "directness" anchor, orthogonal to
# every other dim's anchor -> only directness reassembles (S_d >= 0.3).
_PROMPT = [1.0, 0.0, 0.0, 0.0]
_ALIGNED = [1.0, 0.0, 0.0, 0.0]
_ORTHOGONAL = [0.0, 1.0, 0.0, 0.0]


class _FakeBackend:
    model_version = "stub-e2e"

    def encode(self, texts):
        return [list(_PROMPT)] * len(texts)


def _fake_anchors():
    """Per-dim anchors where directness aligns with the prompt (high S_d) and
    every other dim is orthogonal (low S_d) -> only directness reassembles."""
    dims = ["directness", "context_richness", "exploration",
            "analytical_depth", "iteration", "scope"]
    return {d: {"positive": [list(_ALIGNED if d == "directness"
                                    else _ORTHOGONAL)],
                "negative": []}
            for d in dims}


def test_user_turn_reassembles_and_persists_reference(tmp_path, monkeypatch):
    _db.set_override(str(tmp_path / "e2e.db"))
    import engine

    eng = engine.Engine()
    monkeypatch.setattr(eng, "_get_backend", lambda: _FakeBackend())
    monkeypatch.setattr(eng, "_anchor_vectors_for",
                        lambda backend: _fake_anchors())
    # Isolate the reference path from the pattern-persist plumbing.
    monkeypatch.setattr(eng, "_update_patterns_and_persist",
                        lambda *a, **k: None)

    eng.on_events([{
        "type": "user_turn", "ts": 1.0, "session_id": "s1", "turn_id": "t1",
        "user_message": "Just the short answer, nothing extra",
        "user_chars": 30,
    }])

    # The reference materialized + reassembled the aligned dim only.
    assert eng.reference is not None
    assert eng.reference.windows["directness"].live_count == 1
    assert eng.reference.windows["directness"].seeded is False
    # An orthogonal dim did not reassemble (S_d < 0.3 -> no fold).
    assert eng.reference.windows["scope"].live_count == 0
    # The aligned prompt sits in-territory -> low OOD drift.
    assert eng._ood_drift <= 0.5
    # The pane now surfaces the rolling-reference signal.
    perc = eng.portrait_view()["perception"]
    assert perc["reference_freshness"]["directness"]["live_count"] == 1
    assert perc["ood_drift"] == eng._ood_drift

    # Restart path: a second engine rehydrates the PERSISTED window.
    _db.set_override(str(tmp_path / "e2e.db"))
    eng2 = engine.Engine()
    eng2.reference = None
    eng2._reference_loaded = False
    ref2 = eng2._ensure_reference(backend=_FakeBackend(),
                                  anchor_vectors=_fake_anchors())
    assert ref2.windows["directness"].live_count == 1
    assert ref2.windows["scope"].live_count == 0
