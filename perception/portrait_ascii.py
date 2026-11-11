"""Agent-side ASCII portrait renderer (/mirror portrait draw).

A LEGACY CONSUMER of the shared portrait model (PORTRAIT-AUTHORING §1):
it receives ONLY the normalized evidence dict the /portrait shape carries
(dimensions/targets/confidence + live_expression + morphology) and
projects it onto a deterministic 25x14 character face. No raw text ever
reaches this module; nothing here is invented — every knob reads a shape
field, and all-None evidence renders the documented NEUTRAL resting face
(plain dash eyes, flat mouth, no hair, resting shoulders).

The BASE expression is always the axes-derived shared alias set
(desktop/portrait-engine.mjs DEFAULT_ALIASES): groups openness/focus/warmth
are weighted sums of the six axes; gaze/mouthTension/mouthLift/headTilt
are the shared feature formulas. The transient live_expression channels
modulate that base only while a live gesture is present
(live stage != "none") — the pane's additive live layer (plan §3.4): a
restarted/isolated dashboard half (neutral live expression) still shows
the observed axes in the face; the neutral face appears only when the
evidence is genuinely empty. Face structure knobs read the persisted
morphology (faceWidth/headWidth/jaw/eyeAspect/hairMass/shoulder)
plus the drawing treatment (line_density/echo_strength/
contour_asymmetry). Deterministic: same shape -> same face, always.

7-bit ASCII only (the chat surface is plain text on every platform;
monospace alignment must survive Discord/Telegram/TUI).
"""

W = 25          # grid width
H = 14          # grid height (rows 0..13)
_CENTER = 12
_DIMS = ("directness", "context_richness", "exploration",
         "analytical_depth", "iteration", "scope")
# Pane flank abbreviations (desktop/plugin.js FLANK_ABBR) — the strip uses
# the same three-letter codes so both surfaces read alike.
_ABBR = ("dir", "ctx", "exp", "dep", "itr", "scp")


def _f(value, default=0.0) -> float:
    try:
        v = float(value)
        return v if v == v else default  # NaN guard
    except (TypeError, ValueError):
        return default


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _axis(shape: dict, name: str) -> float:
    dims = shape.get("dimensions") or {}
    v = dims.get(name)
    if v is None:
        return 0.0
    return _clamp(_f(v), -1.0, 1.0)


def _group(shape: dict, name: str) -> float:
    """Shared alias-set group weights (DEFAULT_ALIASES.groups)."""
    if name == "openness":
        v = 0.42 * _axis(shape, "directness") + 0.38 * _axis(shape, "exploration") \
            + 0.20 * _axis(shape, "scope")
    elif name == "focus":
        v = 0.66 * _axis(shape, "analytical_depth") + 0.18 * _axis(shape, "directness") \
            - 0.16 * _axis(shape, "exploration")
    else:  # warmth
        v = 0.58 * _axis(shape, "context_richness") + 0.42 * _axis(shape, "exploration")
    return _clamp(v, -1.0, 1.0)


def _morpho(shape: dict) -> dict:
    m = shape.get("morphology") or {}
    out = {}
    for key in ("faceWidth", "headWidth", "jaw", "brow", "eyeAspect",
                "hairMass", "shoulder", "line_density",
                "contour_asymmetry", "echo_strength"):
        out[key] = _f(m.get(key))
    out["seed"] = bool(m.get("seed"))
    return out


def _grid() -> list:
    return [[" "] * W for _ in range(H)]


def _put(grid, row: int, col: int, ch: str) -> None:
    if 0 <= row < H and 0 <= col < W and ch:
        grid[row][col] = ch


def render_portrait_ascii(shape=None) -> str:
    """Render the current portrait state as an ASCII face + evidence strip.

    ``shape`` is the /portrait shape dict (or None/empty -> the neutral
    resting face with a 'no evidence yet' strip). Never raises: any fault
    degrades to the neutral face plus a fault note.
    """
    try:
        return _render(shape or {})
    except Exception:
        note = "  (render fault — neutral face shown)\n"
        return _render({}) + note


def _render(shape: dict) -> str:
    m = _morpho(shape)
    live = shape.get("live_expression") or {}
    stage = str(live.get("stage") or "none")
    if stage != "none":
        # a live gesture is present in this process: the live channels
        # modulate the axes-derived base (the pane's additive live layer)
        openness = _clamp(_f(live.get("openness")), -1.0, 1.0)
        focus = _clamp(_f(live.get("focus")), -1.0, 1.0)
    else:
        openness = _group(shape, "openness")
        focus = _group(shape, "focus")

    ld = _clamp(m["line_density"], 0.0, 1.0)
    wall = ":" if ld < 0.25 else ("|" if ld < 0.6 else "@")
    top = "." if ld < 0.25 else ("-" if ld < 0.6 else "_")
    echo = m["echo_strength"] >= 0.4

    half = 8 + int(round(_clamp(m["faceWidth"], -1.0, 1.0) * 2))          # 6..10
    top_half = half + int(round(_clamp(m["headWidth"], -1.0, 1.0)))
    chin_half = int(_clamp(half - 2 * int(round(_clamp(m["jaw"], -1.0, 1.0))), 4, 12))
    asym = int(round(_clamp(m["contour_asymmetry"], -1.0, 1.0)))        # right-side widening
    right_half = top_half + asym

    # shared feature formulas (DEFAULT_ALIASES.features)
    gaze = 8.0 * _axis(shape, "directness") + 4.5 * _axis(shape, "exploration")
    eye_shift = int(round(_clamp(gaze / 6.0, -2.0, 2.0)))
    mouth_tension = 4.2 * _axis(shape, "directness") + 2.2 * _axis(shape, "analytical_depth")
    mouth_lift = 9.0 * _group(shape, "warmth")
    head_tilt = 3.2 * _axis(shape, "exploration") + 1.0 * _axis(shape, "scope")
    shear = int(round(_clamp(head_tilt, -3.2, 3.2) / 3.2))              # -1..1

    grid = _grid()
    c = _CENTER

    # hair (rows above the face top; mass scales rows, density scales char)
    hm = _clamp(m["hairMass"], 0.0, 1.0)
    hair_rows = [0, 1] if hm > 0.5 else ([1] if hm > 0.1 else [])
    hair_ch = "o" if ld >= 0.5 else "."
    for r in hair_rows:
        for col in range(c - top_half, c + right_half + 1):
            _put(grid, r, col, hair_ch)

    # face outline: top row 2, walls rows 3..9, chin row 10. The right side
    # carries the contour_asymmetry widening (right_half = top_half + asym),
    # so top, walls and hair share the same asymmetric span.
    for col in range(c - top_half, c + right_half + 1):
        _put(grid, 2, col, top if col not in (c - top_half, c + right_half) else ".")
    for row in range(3, 10):
        _put(grid, row, c - half, wall)
        _put(grid, row, c + half + asym, wall)
        if echo:
            _put(grid, row, c - half - 1, ".")
            _put(grid, row, c + half + 1 + asym, ".")
    for col in range(c - chin_half, c + chin_half + 1):
        _put(grid, 10, col, top if col not in (c - chin_half, c + chin_half) else ".")

    # eyes (row 5): open degree from openness/focus; width from eyeAspect
    eo = 0.82 + _clamp(openness, -1.0, 1.0) * 0.18 - _clamp(focus, -1.0, 1.0) * 0.08
    eye = "o" if eo >= 0.95 else ("-" if eo >= 0.75 else ".")
    wide = m["eyeAspect"] >= 0.5
    narrow = m["eyeAspect"] <= -0.5
    eye_ch = "." if narrow else (eye + "o" if wide and eye == "-" else eye)
    lec = c - 4 + eye_shift
    rec = c + 4 + eye_shift
    _put(grid, 5, lec, eye_ch[0] if eye_ch else ".")
    if len(eye_ch) > 1:
        _put(grid, 5, lec + 1, eye_ch[1])
    _put(grid, 5, rec, eye_ch[0] if eye_ch else ".")
    if len(eye_ch) > 1:
        _put(grid, 5, rec + 1, eye_ch[1])

    # brows (row 4, raised by openness): flat or inward-angled by tension
    brow_lift = int(round(_clamp(openness, 0.0, 1.0) * 2))
    brow_row = 4 - brow_lift
    flat = mouth_tension < 2.5
    _put(grid, brow_row, lec - 1, "-" if flat else "\\")
    _put(grid, brow_row, rec + 1, "-" if flat else "/")

    # mouth (row 8): warm lift -> v, tense -> ~, else flat
    mouth = "v" if mouth_lift >= 1.5 else ("~" if mouth_tension >= 2.5 else "-")
    _put(grid, 8, c, mouth)

    # shoulders (rows 12..13): spread by shoulder morphology
    sh = _clamp(m["shoulder"], -1.0, 1.0)
    span = int(_clamp(round((half + 3) * (1 + sh * 0.25)), 6, 12))
    _put(grid, 12, c - span, wall)
    _put(grid, 12, c + span, wall)
    for col in range(c - span + 1, c + span):
        _put(grid, 12, col, "-")
    _put(grid, 13, c - span - 1, ".")
    for col in range(c - span + 1, c + span):
        _put(grid, 13, col, ".")
    _put(grid, 13, c + span + 1, ".")

    # headTilt shear: nudge the face rows (2..10) as a block; the shoulder
    # base stays put (the head tilts over the resting shoulders).
    if shear:
        for row in range(2, 11):
            new_row = [" "] * W
            for col in range(W):
                target = col + shear
                if 0 <= target < W and grid[row][col] != " ":
                    new_row[target] = grid[row][col]
            grid[row] = new_row

    # strip: the six axes (value/target/confidence) in pane flank codes
    dims = shape.get("dimensions") or {}
    targets = shape.get("targets") or {}
    conf = shape.get("confidence") or {}
    have_evidence = any(dims.get(d) is not None for d in _DIMS)

    def cell(v) -> str:
        if v is None:
            return "  -"
        sign = "+" if v >= 0 else "-"
        return f"{sign}{abs(v):.2f}"

    rows = []
    for i in range(0, 6, 3):
        parts = []
        for j, d in enumerate(_DIMS[i:i + 3]):
            abbr = _ABBR[i + j]
            val = dims.get(d)
            tgt = targets.get(d)
            cf = conf.get(d)
            parts.append(
                f"{abbr} {cell(None if val is None else _f(val))}"
                + (f" tgt {cell(None if tgt is None else _f(tgt))}" if tgt is not None else "")
                + f" c {cell(None if cf is None else _f(cf))}")
        rows.append("  " + " | ".join(parts))
    tail = "evidence present" if have_evidence else "no evidence yet (neutral resting face)"
    rows.append(f"  stage {stage} | seed {str(m['seed']).lower()} | line {ld:.2f} | "
                f"asym {m['contour_asymmetry']:.2f} | {tail}")

    lines = ["  " + "".join(row) for row in grid]
    lines += rows
    return "\n".join(lines)
