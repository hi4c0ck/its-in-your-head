"""Export schema + compact extraction of REAL engine views.

This module never computes a metric. It only selects which fields of the
engine's authoritative view dicts are written to the run JSON so the board
can replay them. Values are copied unchanged.
"""
from __future__ import annotations

SCHEMA_ID = "iyyh-metrics-emulation/v1"

DIMS = ("directness", "context_richness", "exploration",
        "analytical_depth", "iteration", "scope")


def _num(v):
    """Keep engine numbers exact; coerce only numpy-like scalars."""
    if v is None:
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            return v
    return v


def extract_portrait(view: dict) -> dict:
    """Copy the engine portrait_view into board-sized fields, unchanged."""
    if not isinstance(view, dict):
        return {"available": False}
    out = {
        "available": bool(view.get("available")),
        "dimensions": {d: _num((view.get("dimensions") or {}).get(d)) for d in DIMS},
        "targets": {d: _num((view.get("targets") or {}).get(d)) for d in DIMS},
        "confidence": {d: _num((view.get("confidence") or {}).get(d)) for d in DIMS},
        "user": {d: _num((view.get("user") or {}).get(d)) for d in DIMS},
        "agent": {d: _num((view.get("agent") or {}).get(d)) for d in DIMS},
        "live_expression": view.get("live_expression") or {},
        "morphology": view.get("morphology") or {},
    }
    perc = view.get("perception") or {}
    patterns = perc.get("patterns") or []
    out["perception"] = {
        "perception_available": bool(perc.get("perception_available")),
        "ood_drift": _num(perc.get("ood_drift")),
        "reference_freshness": perc.get("reference_freshness"),
        # names + confidence only: the full pattern text is bulky and is not
        # used by the board. These numbers come from the engine unchanged.
        "patterns": [{"name": p.get("name"),
                      "confidence": _num(p.get("confidence")),
                      "evidence": p.get("evidence"),
                      "amplification": _num(p.get("amplification"))}
                     for p in patterns[:8]],
    }
    return out


def extract_drift(view: dict) -> dict:
    if not isinstance(view, dict):
        return {"available": False, "series": {d: [] for d in DIMS}}
    series = view.get("series") or {}
    return {
        "available": bool(view.get("available")),
        "series": {d: [_num(v) for v in (series.get(d) or [])] for d in DIMS},
    }


def _lane_entries(entries, limit=4):
    out = []
    for e in (entries or [])[:limit]:
        if not isinstance(e, dict):
            continue
        out.append({
            "tool_name": e.get("tool_name"),
            "tool_id": e.get("tool_id"),
            "rank": e.get("rank"),
            "value": _num(e.get("value")),
            "level": e.get("level"),
            "state": e.get("state"),
            "momentum": e.get("momentum"),
            "label": e.get("label"),
        })
    return out


def extract_pulse(view: dict) -> dict:
    if not isinstance(view, dict):
        return {"available": False, "lanes": {}, "rows": 0, "upset": {}}
    lanes = view.get("lanes") or {}
    return {
        "available": bool(view.get("available")),
        "schema_version": view.get("schema_version"),
        "rows": len(view.get("rows") or []),
        "lanes": {m: _lane_entries(lanes.get(m)) for m in ("live", "goal", "good", "friction")},
        "upset": view.get("upset") or {},
    }


def summarize(run: dict) -> dict:
    """Display-only summary derived from already-exported engine numbers."""
    turns = run.get("turns") or []
    first, last = {}, {}
    spans, deltas = {}, {}
    for d in DIMS:
        vals = [t["portrait"]["dimensions"].get(d) for t in turns]
        vals = [float(v) for v in vals if isinstance(v, (int, float))]
        first[d] = vals[0] if vals else None
        last[d] = vals[-1] if vals else None
        spans[d] = (max(vals) - min(vals)) if vals else None
        s = next((t["drift"]["series"].get(d) for t in reversed(turns)
                  if t["drift"]["series"].get(d)), [])
        deltas[d] = (max(s) - min(s)) if s else None
    fric = []
    for t in turns:
        u = t["pulse"].get("upset") or {}
        v = u.get("value") if isinstance(u, dict) else None
        if isinstance(v, (int, float)):
            fric.append(float(v))
    confs = [c for t in turns for c in (t["portrait"].get("confidence") or {}).values()
             if isinstance(c, (int, float))]
    return {
        "portrait_first": first,
        "portrait_last": last,
        "portrait_span": spans,
        "max_drift": deltas,
        "friction_peak": max(fric) if fric else None,
        "confidence_mean": (sum(confs) / len(confs)) if confs else None,
    }
