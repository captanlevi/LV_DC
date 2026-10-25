"""lever_params.py: orchestrate's --lever-params (v3 B4 x B5): the frozen line's lever values per platform, validated.
Apart from lever.py so the shaper-path module keeps its import proof (test_lever: random / threading only)."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

PLATFORMS = ("youtube", "twitch", "tiktok")
PARAM_KEYS = ("lambda_per_s", "d_lo_s", "d_hi_s")


def load_params(path) -> tuple[dict, str]:
    """orchestrate --lever-params (the frozen line's values, per platform): ({platform: {lambda_per_s, d_lo_s,
    d_hi_s}}, sha256 of the file). Every platform present, every value a finite number, lambda >= 0 (0: the platform's
    lever is disabled, levi 17:21: no plan, recorded lever_disabled), 0 < d_lo <= d_hi; anything else raises ValueError
    (the window is refused before trio_up)."""
    raw = Path(path).read_bytes()
    try:
        d = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"lever params {path}: not JSON ({e})")
    if not isinstance(d, dict):
        raise ValueError(f"lever params {path}: not an object")
    out = {}
    for pf in PLATFORMS:
        v = d.get(pf)
        if not isinstance(v, dict) or set(v) != set(PARAM_KEYS):
            raise ValueError(f"lever params {path}: {pf} needs exactly {list(PARAM_KEYS)}, got {v!r}")
        if not all(isinstance(v[k], (int, float)) and not isinstance(v[k], bool) and math.isfinite(v[k]) for k in PARAM_KEYS):
            raise ValueError(f"lever params {path}: {pf} has a non-numeric / non-finite value {v!r}")
        if not (v["lambda_per_s"] >= 0 and 0 < v["d_lo_s"] <= v["d_hi_s"]):
            raise ValueError(f"lever params {path}: {pf} out of range {v!r} (lambda >= 0, 0 < d_lo <= d_hi)")
        out[pf] = {k: float(v[k]) for k in PARAM_KEYS}
    return out, hashlib.sha256(raw).hexdigest()
