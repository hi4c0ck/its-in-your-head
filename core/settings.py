"""C4 settings keys, read via the plugin ctx config API.

Verified API (hermes_cli/plugins.py): ctx.get_config(key, default) reads
the plugin's config subtree. If the ctx in use does not expose it
(plain test doubles), DEFAULTS apply — no API is invented here.
"""
import logging

logger = logging.getLogger(__name__)

# C4 keys (land with S5; adaptation.enabled defaults false).
DEFAULTS = {
    "perception.enabled": True,
    "observation.reasoning": True,
    "observation.retain_days": 90,
    "pulse.prototype_cap": 8,
    "pulse.min_labeled": 3,
    "pulse.thresholds.goal": 0.8,
    "pulse.thresholds.good": 0.6,
    "pulse.thresholds.friction": 0.4,
    "adaptation.enabled": False,
    "adaptation.rules": [],
    # S2 adjudication 2026-11-06: values tier-0 | tier-1-onnx (default tier-0)
    "perception.encoder": "tier-0",
    # D3 upscale route B (manual injection): extra bank patterns, each an
    # object {name: str, prompt: [str, ...], agent: [str, ...]}.
    "perception.custom_patterns": [],
    # D3 upscale route A (context): reinforce agent anchors on strong
    # paired observations; minimum amplification to qualify.
    "perception.reinforce": True,
    "perception.reinforce_min_amplification": 0.15,
}


def load(ctx) -> dict:
    """Snapshot all C4 keys from ctx.get_config (fall back to DEFAULTS
    when the ctx has no config API)."""
    out = dict(DEFAULTS)
    get = getattr(ctx, "get_config", None)
    if not callable(get):
        return out
    for key, default in DEFAULTS.items():
        try:
            out[key] = get(key, default)
        except Exception:
            logger.exception("its-in-your-head: settings read failed for %s", key)
    return out


def get(snapshot: dict, key: str):
    return snapshot.get(key, DEFAULTS.get(key))
