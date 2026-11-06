#!/usr/bin/env python3
"""G5 — top-level contract-drift checker (its-in-your-head, P0.2).

Parent-run gate. Checks, against CONTRACTS.md:
  C1  pinned REST routes ⊇ declared router routes (additive OK; pinned missing = fail)
  C2  broadcast_plugin_event basenames ⊆ pinned {tick, pulse, settings}
  C3  DDL copy in dashboard/plugin_api.py == SCHEMA in metrics.py (normalized)
  C4  config_schema keys (checked only once plugin.yaml declares one)
  C7  file-ownership audit (parent supplies --diff-from; off-map files = fail)

Exit 0 = clean. Usage:
  python tools/check_contracts.py [--diff-from <git-ref>]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PINNED_ROUTES = {"/metrics", "/heatmap", "/recent"}          # C1 live
PLANNED_ROUTES = {"/pulse", "/portrait", "/drift", "/context",
                  "/settings", "/reset"}                     # C1 planned (OK to be absent)
PINNED_EVENTS = {"tick", "pulse", "settings"}                # C2
OWNERSHIP = {                                                 # C7
    "lane-a": {"core/", "storage/", "__init__.py", "metrics.py",
               "tests/test_s1_"},
    "lane-b": {"perception/", "tests/test_s2_", "tests/test_s3_"},
    "lane-c": {"pulse/", "adaptation/", "commands.py",
               "dashboard/plugin_api.py", "tests/test_s4_", "tests/test_s5_",
               "tests/test_s7_"},
    "lane-d": {"desktop/", "tests/test_desktop_"},
    "parent": {"plugin.yaml", "CONTRACTS.md", "tools/check_contracts.py",
               "tests/test_structure.py", "tests/acceptance/"},
}
PARENT_OWNED_FILES = {p for f in OWNERSHIP["parent"] for p in [f]}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def check_c1() -> list[str]:
    api = ROOT / "dashboard" / "plugin_api.py"
    text = _read(api)
    declared = set(re.findall(r'@router\.(?:get|post)\(\s*"([^"]+)"', text))
    errs = []
    missing = PINNED_ROUTES - declared
    if missing:
        errs.append(f"C1: pinned routes missing from dashboard: {sorted(missing)}")
    # additive routes outside pinned∪planned = adjudication needed
    unknown = declared - (PINNED_ROUTES | PLANNED_ROUTES)
    if unknown:
        errs.append(f"C1: routes outside pinned∪planned set: {sorted(unknown)} "
                    "(parent adjudication required)")
    return errs


def check_c2() -> list[str]:
    errs = []
    for f in list(ROOT.glob("*.py")) + list(ROOT.glob("dashboard/*.py")) \
            + list(ROOT.glob("desktop/*.js")):
        text = _read(f)
        for m in re.finditer(
                r"broadcast_plugin_event\(\s*[\"'][^\"']*[\"']\s*,\s*[\"']([^\"']+)[\"']",
                text):
            ev = m.group(1)
            if ev not in PINNED_EVENTS:
                errs.append(f"C2: unpinned event '{ev}' in {f.name}")
        for m in re.finditer(r"onEvent\(\s*['\"]plugin\.its-in-your-head\.([a-z_]+)['\"]",
                             text):
            if m.group(1) not in PINNED_EVENTS:
                errs.append(f"C2: onEvent for unpinned event '{m.group(1)}' in {f.name}")
    return errs


def _norm_ddl(s: str) -> str:
    s = re.sub(r"--[^\n]*", "", s)          # strip comments
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\bIF NOT EXISTS\b", "", s)
    return s


def check_c3() -> list[str]:
    errs = []
    m1 = re.search(r'SCHEMA\s*=\s*"""(.*?)"""', _read(ROOT / "metrics.py"), re.DOTALL)
    if not m1:
        errs.append("C3: SCHEMA block not found in metrics.py")
        return errs
    api_text = _read(ROOT / "dashboard" / "plugin_api.py")
    m2 = re.search(r'_(?:SCHEMA|DDL|SCHEMA_COPY)\s*=\s*"""(.*?)"""', api_text, re.DOTALL)
    if not m2:
        # allow inline executescript literal
        m2 = re.search(r'executescript\(\s*"""(.*?)"""', api_text, re.DOTALL)
    if not m2:
        errs.append("C3: no DDL copy found in dashboard/plugin_api.py "
                    "(marked SCHEMA/DDL copy or executescript literal expected)")
    else:
        if _norm_ddl(m1.group(1)) != _norm_ddl(m2.group(1)):
            errs.append("C3: DDL drift between metrics.py SCHEMA and dashboard copy")
    return errs


def check_c4() -> list[str]:
    y = _read(ROOT / "plugin.yaml")
    m = re.search(r"config_schema:\s*(.+)", y)
    if not m:
        return []  # vacuous until S5 lands config_schema
    return ["C4: config_schema present — verify keys against CONTRACTS C4 manually "
            "(checker key-list lands with S5)"]


def check_c7(diff_from: str) -> list[str]:
    import subprocess
    r = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", diff_from],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return [f"C7: git diff --name-only {diff_from} failed: {r.stderr.strip()}"]
    changed = [ln for ln in r.stdout.splitlines() if ln.strip()]
    errs = []
    off = []
    for path in changed:
        norm = path.replace("\\", "/")
        in_lane = any(
            any(norm.startswith(p) for p in files)
            for lane, files in OWNERSHIP.items()
            if lane != "parent"
        )
        in_parent = any(norm.startswith(p) or norm == p
                        for p in PARENT_OWNED_FILES)
        if not (in_lane or in_parent):
            off.append(norm)
    if off:
        errs.append(f"C7: files outside any ownership set: {off}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff-from", default="", help="git ref for C7 audit")
    args = ap.parse_args()
    errs = check_c1() + check_c2() + check_c3() + check_c4()
    if args.diff_from:
        errs += check_c7(args.diff_from)
    for e in errs:
        print("FAIL", e)
    print(f"G5: {'CLEAN' if not errs else str(len(errs)) + ' violation(s)'} "
          f"(checked C1 C2 C3 C4{' C7' if args.diff_from else ''})")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
