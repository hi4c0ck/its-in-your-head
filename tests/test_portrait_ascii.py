"""Unit tests for the /mirror portrait draw ASCII renderer.

The renderer is pure and deterministic: same shape -> same face. These
tests pin the neutral geometry, the axes->feature math (shared alias
formulas), the live-override gate, the morphology knobs, and the
command wiring in commands.py.
"""
import importlib

from perception import portrait_ascii as pa


def _grid(out: str):
    """The 14 face rows of a render (the strip follows the face)."""
    lines = out.splitlines()
    face = [row[2:27] for row in lines[:14]]  # drop the 2-space prefix
    assert all(len(row) == 25 for row in face)
    return face, lines[14:]


def _cell(out: str, row: int, col: int) -> str:
    # face rows carry a 2-space prefix in the rendered output
    return out.splitlines()[row][2 + col]


# --- determinism + neutral geometry -------------------------------------

def test_neutral_deterministic_and_ascii():
    out = pa.render_portrait_ascii(None)
    assert out == pa.render_portrait_ascii({}) == pa.render_portrait_ascii(None)
    for ch in out:
        assert ord(ch) < 128, "chat surface is 7-bit ASCII"
    face, strip = _grid(out)
    assert "no evidence yet" in " ".join(strip)
    assert face[0] == face[1] == " " * 25  # no hair at neutral mass
    # mirror symmetry of the neutral face: every row is its own reversal
    for row in face:
        assert row == row[::-1], row


def test_neutral_face_knobs():
    out = pa.render_portrait_ascii({})
    assert _cell(out, 2, 12) == "."            # face top
    assert _cell(out, 3, 4) == ":"             # low-density wall
    assert _cell(out, 5, 8) == "-"             # open-dash eyes (neutral eo=0.82)
    assert _cell(out, 5, 16) == "-"
    assert _cell(out, 4, 7) == "-"             # flat brows
    assert _cell(out, 4, 17) == "-"
    assert _cell(out, 8, 12) == "-"            # flat mouth
    assert _cell(out, 12, 1) == ":"            # shoulder span tips
    assert _cell(out, 10, 12) == "."           # chin


def test_all_none_dimensions_stay_neutral():
    shape = {"dimensions": {d: None for d in pa._DIMS}}
    assert pa.render_portrait_ascii(shape) == pa.render_portrait_ascii({})


# --- axes -> feature formulas (the shared alias set) ---------------------

def test_warmth_lifts_mouth():
    shape = {"dimensions": {"context_richness": 1.0, "exploration": 1.0}}
    out = pa.render_portrait_ascii(shape)
    # warmth 1.0 -> lift 9.0 >= 1.5 -> "v"; exploration 1.0 shears row 8 +1
    assert _cell(out, 8, 13) == "v"


def test_tension_waves_mouth_and_angles_brows():
    shape = {"dimensions": {"directness": 1.0, "analytical_depth": 1.0}}
    out = pa.render_portrait_ascii(shape)
    assert _cell(out, 8, 12) == "~"            # tension 6.4 >= 2.5
    # gaze +1 -> eyes 9/17; inward-angled brows ride them: openness
    # 0.42 -> lift 1 -> brow row 3 at cols lec-1=8, rec+1=18
    assert _cell(out, 3, 8) == "\\"
    assert _cell(out, 3, 18) == "/"


def test_directness_shifts_gaze():
    neutral = pa.render_portrait_ascii({})
    out = pa.render_portrait_ascii({"dimensions": {"directness": 1.0}})
    assert _cell(out, 5, 9) == "-"             # eyes shifted +1 (gaze 8.0)
    assert out != neutral


def test_focus_narrows_eyes():
    shape = {"dimensions": {"analytical_depth": 1.0}}
    out = pa.render_portrait_ascii(shape)
    # focus 0.66 -> eo = 0.82 - 0.08*0.66 = 0.767 < 0.95: stays dash,
    # but focus >= 0.5 with low openness must not open the eyes
    assert _cell(out, 5, 8) == "-"


def test_openness_opens_eyes_and_raises_brows():
    shape = {"dimensions": {"directness": 1.0, "exploration": 1.0, "scope": 1.0}}
    out = pa.render_portrait_ascii(shape)
    # openness 1.0 -> wide-open "o" eyes; gaze 12.5 -> shift 2 -> eyes at
    # 10/18, then headTilt (exp+scope) shears row 5 +1 -> 11/19. Tension
    # 4.2 -> angled brows ride row 2 at 10/20 (post-shear).
    assert _cell(out, 5, 11) == "o"
    assert _cell(out, 5, 19) == "o"
    assert _cell(out, 2, 10) == "\\"
    assert _cell(out, 2, 20) == "/"


def test_headtilt_shears_the_face():
    plain = pa.render_portrait_ascii({})
    tilted = pa.render_portrait_ascii(
        {"dimensions": {"exploration": 1.0, "scope": 1.0}})
    assert tilted.splitlines()[3] != plain.splitlines()[3]  # sheared row
    # shoulders stay un-sheared: identical shoulder rows
    assert tilted.splitlines()[12] == plain.splitlines()[12]


# --- live-expression gate (pane §3.4 additive layer) ---------------------

def test_live_stage_overrides_axes_expression():
    axes = {"dimensions": {"context_richness": 1.0, "exploration": 1.0}}
    base = pa.render_portrait_ascii(axes)
    live = dict(axes, live_expression={"openness": 0.0, "focus": 0.0,
                                        "resonance": 0.5, "tension": 0.5,
                                        "intensity": 0.0, "confidence": 0.0,
                                        "stage": "working", "updated_at": 1.0})
    out = pa.render_portrait_ascii(live)
    assert out != base
    assert "stage working" in out


def test_neutral_live_keeps_axes_expression():
    axes = {"dimensions": {"context_richness": 1.0, "exploration": 1.0}}
    out = pa.render_portrait_ascii({
        **axes,
        "live_expression": {"openness": 0.0, "focus": 0.0, "resonance": 0.5,
                            "tension": 0.5, "intensity": 0.0,
                            "confidence": 0.0, "stage": "none"}})
    assert out == pa.render_portrait_ascii(axes)


# --- morphology knobs ------------------------------------------------------

def test_line_density_and_hair():
    shape = {"morphology": {"line_density": 0.9, "hairMass": 0.9}}
    out = pa.render_portrait_ascii(shape)
    assert _cell(out, 3, 4) == "@"             # dense wall
    assert _cell(out, 0, 12) == "o"            # two hair rows, dense char
    assert _cell(out, 1, 12) == "o"
    sparse = pa.render_portrait_ascii({"morphology": {"hairMass": 0.3}})
    assert _cell(sparse, 0, 12) == " "         # one hair row only (row 1)
    assert _cell(sparse, 1, 12) == "."


def test_face_width_knobs():
    wide = pa.render_portrait_ascii(
        {"morphology": {"faceWidth": 1.0}})
    narrow = pa.render_portrait_ascii(
        {"morphology": {"faceWidth": -1.0}})
    # half = 8 + faceWidth*2: wide -> walls at 2/22, narrow -> 6/18
    assert _cell(wide, 3, 2) == ":"
    assert _cell(wide, 3, 22) == ":"
    assert _cell(narrow, 3, 6) == ":"
    assert _cell(wide, 2, 12) == "."           # low-density top span


def test_asymmetry_widens_right_side():
    out = pa.render_portrait_ascii(
        {"morphology": {"contour_asymmetry": 1.0}})
    assert _cell(out, 3, 21) == ":"             # right wall pushed out by 1
    row3 = out.splitlines()[3][2:27]
    assert row3 != row3[::-1]                   # asymmetric: not a mirror


def test_echo_doubling():
    plain = pa.render_portrait_ascii({})
    echoed = pa.render_portrait_ascii(
        {"morphology": {"echo_strength": 0.5}})
    assert _cell(echoed, 3, 3) == "."          # faint outside echo column
    assert _cell(plain, 3, 3) == " "


def test_targets_appear_in_strip():
    out = pa.render_portrait_ascii({
        "dimensions": {"directness": 0.42},
        "targets": {"directness": 0.80},
        "confidence": {"directness": 0.71}})
    strip = " ".join(out.splitlines()[14:])
    assert "dir +0.42 tgt +0.80 c +0.71" in strip
    assert "evidence present" in strip


def test_fault_degrades_to_neutral():
    out = pa.render_portrait_ascii({"dimensions": 42})  # wrong type -> fault
    assert "render fault" in out


# --- command wiring ---------------------------------------------------------

def test_command_portrait_draw(tmp_path):
    """The /mirror dispatch routes `portrait draw` to the renderer."""
    import dashboard.plugin_api as api
    import commands
    importlib.reload(api)
    api.set_db_path(tmp_path / "data.db")

    class FakeCtx:
        def __init__(self):
            self.commands = {}

        def register_command(self, name, handler, description="", args_hint=""):
            self.commands[name] = handler

    ctx = FakeCtx()
    commands.register_commands(ctx)
    out = ctx.commands["mirror"]("portrait draw")
    assert out.startswith("portrait draw:")
    assert "no evidence yet" in out
    assert not out.lstrip().startswith("{")
    # the plain portrait view is unchanged
    plain = ctx.commands["mirror"]("portrait")
    assert plain.startswith("portrait:")
    # an unknown portrait arg falls through to the text view
    assert ctx.commands["mirror"]("portrait bogus").startswith("portrait:")
