"""The trace regime's replay (LVQoE restart_2026-10-21_prereg_levi.md §9; levi's rulings efb5e4cc on peter's estimate
d77f8fcb). Pure: the per-session draw, the session's trace plan, its per-second series from the cache, the clip and the
tc changes the shaper applies. exp.py runs trace_episode_generator through the same loop and make targets as the
state walk (one `make slow` at the start, then `make rate` per change); orchestrate draws the regime per session.

 - The draw: each record trio session is trace-driven with probability TRACE_P, from its own generator seeded by the
   session key (random.Random(f"trace:{TRACE_SEED}:{key}")): independent of coverage, time, the global random and
   every other per-session draw. Single mode: out of scope (ruling 4).
 - The plan: a source with equal weight, a random trace of it and a random start offset; further random traces of the
   same source, each used once (no looping), concatenated from their start until the session is covered. Seeded by
   the session key on a generator of its own; ids, offsets and junction times are recorded.
 - Applied: the trace's per-second kbit/s, clipped to [state 1's rate_min, upper]; upper = min(the window's probe
   goodput, the configured link capacity) / N (ruling 3, fixed per window by orchestrate). Delay: 40 ms x the
   session's delay / loss factor (§2b; no source records an RTT); loss 0. A change only when the clipped kbit (rounded)
   differs from the one set.
 - A trace row has no state (null): rate_band_state is the nominal state whose band holds the clipped rate (half-open
   [rate_min, rate_max), the top state's max included; outside every band the nearest by log-rate), and the low-state
   hold keys on it (ruling 2).
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from utils.dataModels import NetStat  # noqa: E402
from utils.shaping import SCENARIOS  # noqa: E402

TRACE_RULE = "restart 2026-10-21 §9 (levi efb5e4cc): per-second trace rate clipped to [state 1 min, upper], delay 40 ms x dl_factor, loss 0"
TRACE_P = 0.10
TRACE_SEED = 0
TRACE_DELAY_MS = 40
MANIFEST = HERE / "manifest.json"
BAND_RULE = "half-open [rate_min, rate_max), the top state's max included; outside every band the nearest by log-rate"


def cache_dir() -> Path:
    return Path(os.environ.get("LVDC_TRACE_CACHE") or Path.home() / "Desktop" / "LV_DC_traces_cache")


def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(Path(path).read_text())


def drawn(key: str, p: float = TRACE_P, seed: int = TRACE_SEED) -> bool:
    """The session's trace draw: its own generator, seeded by the session key (never the global random)."""
    return random.Random(f"trace:{seed}:{key}").random() < p


def lower_kbit() -> int:
    """The lower clip: the lowest state's rate_min (§9)."""
    return min(sc.rate_min for sc in SCENARIOS.values())


def bands() -> list[tuple[str, int, int]]:
    return sorted(((s, sc.rate_min, sc.rate_max) for s, sc in SCENARIOS.items()), key=lambda x: x[1])


def rate_band_state(kbit: float) -> str:
    """The nominal state whose rate band holds kbit (BAND_RULE)."""
    bs = bands()
    for i, (s, lo, hi) in enumerate(bs):
        if lo <= kbit < hi or (i == len(bs) - 1 and kbit == hi):
            return s
    x = math.log(max(kbit, 1e-9))
    return min(bs, key=lambda b: min(abs(x - math.log(b[1])), abs(x - math.log(b[2]))))[0]


def plan(key: str, session_s: float, manifest: dict, seed: int = TRACE_SEED, source: str | None = None) -> dict:
    """The session's trace plan (module docstring): source, segments [{trace, offset_s, start_s, seconds}], junctions.
    source: forced (G1's segment rotates one source per platform), else drawn with equal weight."""
    rng = random.Random(f"trace-plan:{seed}:{key}")
    drawn_source = rng.choice(sorted(manifest["sources"]))
    source = source or drawn_source
    files = sorted((f for f in manifest["sources"][source]["files"] if (f.get("seconds") or 0) >= 2),
                   key=lambda f: f["name"])
    if not files:
        raise ValueError(f"trace source {source}: no usable trace")
    pool = list(files)
    first = pool.pop(rng.randrange(len(pool)))
    offset = rng.randrange(0, max(1, int(first["seconds"]) - 1))
    segs = [{"trace": first["name"], "sha256": first["sha256"], "offset_s": offset, "start_s": 0,
             "seconds": int(first["seconds"]) - offset}]
    covered = segs[0]["seconds"]
    while covered < session_s and pool:
        f = pool.pop(rng.randrange(len(pool)))
        segs.append({"trace": f["name"], "sha256": f["sha256"], "offset_s": 0, "start_s": covered,
                     "seconds": int(f["seconds"])})
        covered += int(f["seconds"])
    return {"rule": TRACE_RULE, "source": source, "seed": f"trace-plan:{seed}:{key}", "session_s": session_s,
            "segments": segs, "junctions_s": [s["start_s"] for s in segs[1:]], "covered_s": covered,
            "short": covered < session_s, "manifest_schema": manifest.get("schema")}


def series(pl: dict, cache: Path, manifest: dict) -> list[float]:
    """The plan's per-second kbit/s series, its segments concatenated (the cache read through build_manifest)."""
    from traces.build_manifest import load_series
    out: list[float] = []
    by_name = {f["name"]: f for f in manifest["sources"][pl["source"]]["files"]}
    for s in pl["segments"]:
        xs = load_series(cache, pl["source"], by_name[s["trace"]])
        out.extend(xs[s["offset_s"]:s["offset_s"] + s["seconds"]])
    return out


def clipped(kbit: float, lower: float, upper: float) -> tuple[int, str | None]:
    """(the rate set, the clip side or None)."""
    if kbit < lower:
        return int(round(lower)), "low"
    if kbit > upper:
        return int(round(upper)), "high"
    return int(round(kbit)), None


def segment_at(pl: dict, t: float) -> dict:
    return next((s for s in reversed(pl["segments"]) if s["start_s"] <= t), pl["segments"][0])


def trace_episode_generator(xs: list[float], pl: dict, lower: float, upper: float, delay_ms: int, total_s: float):
    """One NetStat per tc change over total_s seconds (module docstring): walk_step 0 for the first change (make slow:
    rate, delay, loss), k >= 1 for each later one (make rate). state None, rate_band_state on the set rate."""
    if upper < lower:
        raise ValueError(f"upper clip {upper} below the lower {lower}")
    n = min(len(xs), int(math.ceil(total_s)))
    k, t0, cur = 0, 0, None
    changes = []
    for t in range(n):
        rate, side = clipped(xs[t], lower, upper)
        if cur is None or rate != cur[0]:
            if cur is not None:
                changes.append((t0, t - t0, cur))
            t0, cur = t, (rate, side, xs[t])
    if cur is not None:
        changes.append((t0, n - t0, cur))
    for t, dur, (rate, side, raw) in changes:
        seg = segment_at(pl, t)
        yield NetStat(rate=rate, duration=float(dur), delay_ms=int(delay_ms), loss_pct=0.0, state=None, walk_step=k,
                      regime="trace", rate_band_state=rate_band_state(rate), trace_source=pl["source"],
                      trace_id=seg["trace"], trace_offset_s=seg["offset_s"] + (t - seg["start_s"]),
                      trace_kbit=round(raw, 3), clip=side)
        k += 1


def clip_shares(xs: list[float], lower: float, upper: float, total_s: float) -> dict:
    """The session's clipped share of seconds, low / high separately (ruling 3)."""
    n = min(len(xs), int(math.ceil(total_s)))
    lo = sum(1 for x in xs[:n] if x < lower)
    hi = sum(1 for x in xs[:n] if x > upper)
    return {"seconds": n, "low_s": lo, "high_s": hi, "low_share": round(lo / n, 4) if n else None,
            "high_share": round(hi / n, 4) if n else None}
