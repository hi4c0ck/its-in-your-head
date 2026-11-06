"""Pure SVG glyph renderer for a normalized portrait (S3).

Six values in [0, 1] (+ six confidences) -> one deterministic SVG string:
a hexagonal silhouette whose vertices sit at value * radius along six
pinned axes, plus an inner confidence silhouette. Same input bytes in ->
same string out (fixed float formatting, pinned dimension order, no RNG,
no clock). Geometry interpolates smoothly: ``interpolate_portrait`` is
linear in t, so moving between two states sweeps the silhouette
monotonically with no jumps.

Zero knowledge of Hermes APIs. Output is embeddable in a desktop pane:
no external references (no href/xlink/image), no scripts, no fonts.
Silhouette only — no text labels are drawn.
"""
from __future__ import annotations

import math

from .anchors import DIMENSIONS

_SIZE = 96
_CX = _CY = _SIZE / 2.0
_RADIUS = 38.0
_INNER_RADIUS = 30.0


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _ordered(values, confidences=None):
    """Normalize dict-or-sequence input to pinned-order lists in [0, 1]."""
    if isinstance(values, dict):
        vals = [_clip01(values[d]) for d in DIMENSIONS]
    else:
        vals = [_clip01(v) for v in values]
        if len(vals) != len(DIMENSIONS):
            raise ValueError(f"need {len(DIMENSIONS)} values, got {len(vals)}")
    if confidences is None:
        confs = [0.0] * len(DIMENSIONS)
    elif isinstance(confidences, dict):
        confs = [_clip01(confidences[d]) for d in DIMENSIONS]
    else:
        confs = [_clip01(c) for c in confidences]
        if len(confs) != len(DIMENSIONS):
            raise ValueError(f"need {len(DIMENSIONS)} confidences, got {len(confs)}")
    return vals, confs


def _points(vals, radius: float) -> str:
    pts = []
    for i, v in enumerate(vals):
        angle = -math.pi / 2.0 + i * (2.0 * math.pi / len(DIMENSIONS))
        r = radius * _clip01(v)
        x = _CX + r * math.cos(angle)
        y = _CY + r * math.sin(angle)
        pts.append(f"{x:.3f},{y:.3f}")
    return " ".join(pts)


def interpolate_portrait(old, new, t: float) -> dict:
    """Linear interpolation between two portraits — smooth geometry path.

    ``old``/``new`` are {dim: value} dicts (or 6-sequences). t is clamped
    to [0, 1]; t=0 is exactly ``old``, t=1 exactly ``new``.
    """
    t = _clip01(t)
    ov, _ = _ordered(old)
    nv, _ = _ordered(new)
    return {d: _clip01(o + (n - o) * t) for d, o, n in zip(DIMENSIONS, ov, nv)}


def interpolate_confidences(old, new, t: float) -> dict:
    t = _clip01(t)
    _, oc = _ordered([0.5] * 6, old)
    _, nc = _ordered([0.5] * 6, new)
    return {d: _clip01(o + (n - o) * t) for d, o, n in zip(DIMENSIONS, oc, nc)}


def render_svg(values, confidences=None, *, width: int = _SIZE, height: int = _SIZE) -> str:
    """Deterministic SVG string for a normalized portrait. Pure function."""
    vals, confs = _ordered(values, confidences)
    outer = _points(vals, _RADIUS)
    inner = _points(confs, _INNER_RADIUS)
    base = _points([1.0] * len(DIMENSIONS), _RADIUS)
    # Fixed attribute order + fixed float precision => byte-identical output.
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_SIZE} {_SIZE}" '
        f'width="{int(width)}" height="{int(height)}" role="img">'
        f'<polygon points="{base}" fill="none" stroke="currentColor" stroke-opacity="0.25"/>'
        f'<polygon points="{outer}" fill="currentColor" fill-opacity="0.35" '
        f'stroke="currentColor" stroke-opacity="0.8"/>'
        f'<polygon points="{inner}" fill="none" stroke="currentColor" stroke-opacity="0.6"/>'
        f'</svg>'
    )


def render_portrait_svg(portrait_snapshot: dict, **kw) -> str:
    """Render from ``Portrait.snapshot()`` / ``normalized()`` shape."""
    if "values" in portrait_snapshot:
        return render_svg(portrait_snapshot["values"],
                          portrait_snapshot.get("confidences"), **kw)
    if "observed" in portrait_snapshot:
        obs = portrait_snapshot["observed"]
        return render_svg(obs["values"], obs.get("confidence"), **kw)
    raise ValueError("snapshot must contain 'values' or 'observed'")
