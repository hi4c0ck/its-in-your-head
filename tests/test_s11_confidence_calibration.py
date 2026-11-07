"""S11: L11 confidence calibration - margin-aware, data-fitted constants.

Pins the live-data calibration (23-row snapshot): margin-gate curve,
recalibrated alpha, unchanged 7-day half-life, exact legacy
reproduction when margin=None, and a read-only replay of the live
rows showing a wider display spread with no age inversion.
"""
import math
import sqlite3
import statistics
from collections import Counter
from pathlib import Path

import pytest

from perception import patterns

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "data" / "live-snapshot.db"
MEDIAN_MARGIN = 0.023042368115878453


def _legacy(n, agreement, age):
    value = (patterns.CONFIDENCE_ALPHA * math.log(1.0 + n) * agreement
             * math.exp(-patterns.CONFIDENCE_LAMBDA * age))
    return max(0.0, min(1.0, value))


def test_constants_pinned():
    assert patterns.CONFIDENCE_MARGIN_K == 0.0332
    assert patterns.CONFIDENCE_ALPHA_CAL == 1.639
    # 7-day half-life intent preserved.
    assert patterns.CONFIDENCE_HALF_LIFE_S == 7 * 24 * 60 * 60
    assert patterns.CONFIDENCE_LAMBDA == pytest.approx(
        math.log(2.0) / (7 * 24 * 60 * 60))


def test_margin_gate_curve():
    # K = median/ln2 -> median margin gates at ~0.5x.
    assert patterns.margin_gate(MEDIAN_MARGIN) == pytest.approx(0.5, abs=0.01)
    assert patterns.margin_gate(0.002) == pytest.approx(0.058, abs=0.01)
    assert patterns.margin_gate(0.067) == pytest.approx(0.867, abs=0.01)
    assert patterns.margin_gate(0.20) == pytest.approx(0.998, abs=0.01)
    # >=0.20 lands at/above the ~0.97x target (0.998x exceeds it).
    assert patterns.margin_gate(0.20) >= 0.97
    assert patterns.margin_gate(0.0) == 0.0
    assert patterns.margin_gate(-0.05) == 0.0  # non-top margins clamp
    vals = [patterns.margin_gate(m) for m in (0.0, 0.002, 0.01, 0.023, 0.067, 0.2, 0.5)]
    assert vals == sorted(vals)  # monotone


def test_margin_none_reproduces_legacy_exactly():
    fixtures = [
        (0, 0.0, 0.0), (2, 0.5, 0.0), (2, 0.5, 3600.0), (3, 0.667, 7200.0),
        (4, 0.25, 100000.0), (5, 0.2, 604800.0), (10, 1.0, 0.0),
        (7, 0.9, 12345.0),
    ]
    for n, agr, age in fixtures:
        assert patterns.confidence_from_evidence(n, agr, age) == _legacy(n, agr, age)
        assert patterns.confidence_from_evidence(
            n, agr, age, margin=None) == _legacy(n, agr, age)
        # amplification alone must not change the legacy branch.
        assert patterns.confidence_from_evidence(
            n, agr, age, margin=None, amplification=-0.3) == _legacy(n, agr, age)
        # amplification is symmetric/ignored in the calibrated branch too.
        assert patterns.confidence_from_evidence(
            n, agr, age, margin=0.023, amplification=-0.3) == (
            patterns.confidence_from_evidence(n, agr, age, margin=0.023))


def test_fresh_ev2_median_margin_target():
    fresh = patterns.confidence_from_evidence(2, 0.5, 0, margin=MEDIAN_MARGIN)
    assert 0.4 <= fresh <= 0.5


def test_calibrated_monotone_no_age_inversion():
    # Identical inputs: older evidence never scores above fresher.
    ages = [0, 3600, 86400, 7 * 86400]
    vals = [patterns.confidence_from_evidence(2, 0.5, a, margin=0.023)
            for a in ages]
    assert vals == sorted(vals, reverse=True)
    assert vals[0] > vals[-1]
    # Identical inputs: larger margin never scores lower.
    vals = [patterns.confidence_from_evidence(3, 0.6, 0, margin=m)
            for m in (0.002, 0.023, 0.067, 0.2)]
    assert vals == sorted(vals)


def _live_rows():
    con = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
    try:
        return list(con.execute(
            "SELECT ts, top_pattern, top_margin, top_amplification, "
            "evidence, confidence FROM pattern_affinity_history ORDER BY ts, id"
        ))
    finally:
        con.close()


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="live snapshot absent")
def test_live_replay_spread_and_dominance():
    rows = _live_rows()
    assert len(rows) == 23
    ref_ts = max(r[0] for r in rows)

    # CR dominance is a top-pattern (affinity) property; the confidence
    # math must not change it. Assert the recorded dominance is intact.
    counts = Counter(r[1] for r in rows)
    assert counts["context_richness"] == 17
    assert counts.most_common(1)[0][0] == "context_richness"

    legacy, calibrated = [], []
    for ts, _pat, margin, amp, ev, conf in rows:
        # Engine computed stored confidences at insertion with age~0.
        base = patterns.CONFIDENCE_ALPHA * math.log(1.0 + ev)
        agr = (conf / base) if base and conf is not None else 0.0
        legacy.append(conf)
        calibrated.append(patterns.confidence_from_evidence(
            ev, min(1.0, max(0.0, agr)), ref_ts - ts,
            margin=margin, amplification=amp))

    # Display confidence spreads under the calibrated formula.
    assert statistics.pstdev(calibrated) > statistics.pstdev(legacy)
    assert len(set(round(c, 4) for c in calibrated)) > len(
        set(round(c, 4) for c in legacy))

    # No age inversion in replay: every row's age-decayed calibrated
    # value is at/below its own fresh (age 0) value, and the freshest
    # row (age 0 at ref) is not below a strictly-older identical-input
    # value (monotonicity covered synthetically above).
    for (ts, _p, margin, amp, ev, conf), new in zip(rows, calibrated):
        base = patterns.CONFIDENCE_ALPHA * math.log(1.0 + ev)
        agr = min(1.0, max(0.0, conf / base)) if base else 0.0
        fresh = patterns.confidence_from_evidence(
            ev, agr, 0, margin=margin, amplification=amp)
        assert new <= fresh + 1e-12
