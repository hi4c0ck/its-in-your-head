"""Emulation harness: synthetic events -> REAL engine -> exported snapshots.

Invariants:
  * every number in the output comes from engine.portrait_view(),
    engine.drift_view() or engine.pulse_view() (no re-scoring here);
  * no real user text: prompts/reasoning are short core phrases from the
    repo's own anchor + feedback tables (see stories.py);
  * the engine is driven through the same on_events() batch feed the metrics
    worker uses, against a temporary profile DB.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import schema, stories

ROOT = Path(__file__).resolve().parents[2]
TURN_SECONDS = 40.0  # realistic cadence; 80 turns ~53 min vs 1h value half-life


def _repo_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return "unknown"


def _engine_block_sha() -> str:
    """sha12 of the production portrait engine block, for board provenance."""
    import hashlib
    src = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    a = src.index("// IYH-PORTRAIT-ENGINE-START")
    b = src.index("// IYH-PORTRAIT-ENGINE-END")
    return hashlib.sha256(src[a + len("// IYH-PORTRAIT-ENGINE-START"):b]
                          .encode("utf-8")).hexdigest()[:12]


def run_story(story_id: str, turns: int = 80, seed: int = 8117,
              include_provenance: bool = True, clock_base: float | None = None) -> dict:
    """Run one story through the real engine and return the run dict."""
    if story_id not in stories.STORIES:
        raise ValueError("unknown story: " + story_id)
    turns = max(1, min(100, int(turns)))

    # The repo root is importable when run via `python -m tests.metrics_emulation.cli`.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import metrics
    import engine
    from pulse import feedback as pulse_feedback

    story = stories.STORIES[story_id]
    drawing = dict(story["drawing"])
    corners = set(stories.corners_for(story_id, turns))
    rng = random.Random(seed)
    # End the synthetic timeline at "now" so capsule ages are real and the
    # 1h accumulation half-life produces graded values, not saturated 1.0s.
    base = (clock_base if clock_base is not None
            else time.time() - turns * TURN_SECONDS)

    run = {
        "schema": schema.SCHEMA_ID,
        "run": {"story": story_id, "label": story["label"], "seed": seed,
                "turns": turns, "drawing": drawing},
        "phases": [{"id": p["id"], "label": p["label"],
                    "from": round(p["start"] * (turns - 1)),
                    "to": round(p["end"] * (turns - 1)) - 1}
                   for p in story["phases"]],
        "corners": [],
        "turns": [],
    }

    with tempfile.TemporaryDirectory(prefix="iyyh-emul-") as td:
        metrics.set_db_path(Path(td) / "data.db")
        previous_engine = engine._engine
        engine._engine = None
        try:
            eng = engine.Engine()  # default tier-0 backend: no ONNX worker
            for i in range(turns):
                phase = stories.phase_at(story_id, i, turns)
                prompt = rng.choice(phase["prompts"])
                tool = rng.choice(phase["tools"])
                status = rng.choice(phase["statuses"])
                reasoning = rng.choice(phase["reasoning"])
                tid = "t%d" % i
                # Realistic session cadence: ~40 s per turn (80 turns ~53 min)
                # against PULSE_VALUE_HALF_LIFE_S=3600, so accumulation values
                # decay progressively instead of saturating at 1.0.
                ts = base + i * TURN_SECONDS
                batch = [
                    {"type": "turn", "ts": ts, "session_id": "emul",
                     "turn_id": tid, "user_message": prompt,
                     "user_chars": len(prompt)},
                    {"type": "tool_start", "ts": ts + 1.0, "session_id": "emul",
                     "turn_id": tid, "tool_name": tool},
                    {"type": "tool_end", "ts": ts + 2.0, "session_id": "emul",
                     "turn_id": tid, "tool_name": tool, "status": status,
                     "duration_ms": 12 + (i % 7)},
                    {"type": "reasoning_seg", "ts": ts + 3.0,
                     "session_id": "emul", "turn_id": tid,
                     "reasoning_text": reasoning,
                     "reasoning_chars": len(reasoning), "reasoning_deltas": 1},
                ]
                # Real collector order: core/collector.py::_on_pre_llm_call
                # enqueues the turn, then _apply_explicit_feedback() labels the
                # PRECEDING capsule BEFORE the worker drains the batch. Use the
                # same real pulse.feedback functions (no reimplementation), so
                # GOOD/FRICTION prototype banks receive the story's explicit
                # "thanks" / "still broken" labels exactly as in the plugin.
                eng.ensure_pulse_hydrated()
                if pulse_feedback.detect_feedback(prompt) is not None:
                    pulse_feedback.apply_feedback(
                        eng.protos, eng.capsule_state(), prompt)

                eng.on_events(batch)

                portrait = schema.extract_portrait(eng.portrait_view())
                drift = schema.extract_drift(eng.drift_view())
                pulse = schema.extract_pulse(eng.pulse_view())
                turn_rec = {
                    "i": i,
                    "phase": phase["id"],
                    "event_summary": {"tools": [tool], "status": status,
                                      "prompt_core": prompt},
                    "portrait": portrait, "drift": drift, "pulse": pulse,
                }
                run["turns"].append(turn_rec)
                if i in corners:
                    run["corners"].append({
                        "turn": i, "phase": phase["id"], "label": phase["label"],
                        "portrait": portrait, "pulse": pulse,
                    })
            eng.on_events([{"type": "session_end", "ts": base + turns * 2.0,
                            "session_id": "emul"}])
        finally:
            engine._engine = previous_engine
            # storage override is a plain module global; None restores the
            # real profile path (metrics.set_db_path types it Path|str only).
            import storage.database
            storage.database.set_override(None)

    run["summary"] = schema.summarize(run)
    if include_provenance:
        run["provenance"] = {
            "repo_commit": _repo_commit(),
            "python": ".".join(map(str, sys.version_info[:3])),
            "portrait_engine_sha": _engine_block_sha(),
            "engine_module": "engine.py (real on_events + views)",
        }
    return run


def write_run(run: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = "%s-%d.json" % (run["run"]["story"], run["run"]["seed"])
    path = out_dir / name
    path.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_run(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
