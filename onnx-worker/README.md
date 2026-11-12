# its-in-your-head ONNX worker

Isolated CPU-only JSON-lines inference server for the Tier-1 ONNX backend.
Kept separate from the plugin so the main process stays dependency-light.

## Environment

Built/ensured through the public Hermes PM facade (never hand-installed):

```python
from pathlib import Path
import pm

# Explicit provisioning — only /mirror engine enable and maintainer
# --repin do this:
python = pm.ensure_project_environment(
    "its-in-your-head-onnx",
    project=Path("<this dir>"),
    explicit=True,
)

# Read-only lookup — the encode path (perception/worker_client.py)
# uses this and never provisions; None means the env is not
# installed and Tier-0 is used:
python = pm.environment_python("its-in-your-head-onnx")
```

`uv.lock` is committed (maintainer-generated). If `pm` is unavailable
the plugin reports Tier-1 "runtime unavailable" and Tier-0 is used.

The worker subprocess runs with an isolated env (minimal allowlist —
PATH/HOME/LANG/LC_ALL plus SYSTEMROOT/TEMP/TMP on Windows), so it
never inherits Hermes provider keys, `*_TOKEN` / `*_SECRET`,
`PYTHONPATH` or `PYTHONHOME`.

## Protocol

JSON-lines on stdin/stdout (pinned, see plan P3):

- `{"op":"embed","texts":[...],"request_id":"r1"}` ->
  `{"request_id":"r1","vectors":[[f,...]],"model_version":"...","dim":384,"latency_ms":...}`
- `{"op":"ping","request_id":"p1"}` ->
  `{"request_id":"p1","ok":true,"model_version":"...","provider":"CPUExecutionProvider"}`
- `{"op":"shutdown"}` -> exit
- errors -> `{"request_id":...,"error":"<class>: <msg>"}`

Usage: `python worker.py <model_dir> <manifest.json>` — verifies manifest
sha256 hashes on startup, loads the ONNX session lazily with
CPUExecutionProvider, tokenizes via the `tokenizers` library against
`tokenizer.json` (no `transformers` dependency), mean-over-attention-mask
pooling + L2 normalization per the manifest `embedding` block.
