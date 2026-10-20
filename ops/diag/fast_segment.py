"""The dry run's @fast segment (LVQoE prereg Addendum Q am. 17, landing condition) and restart §2's walk segment (the
platform's base matrix, the walk's 2 s steps between its 12-30 s re-draws; both segments carry the walk and a delay / loss
factor per slot, as the collector runs them): each slot's platform's @fast family
(utils/shaping FAMILY_BASE + "@fast") drives its class for the segment, from a seeded run of the collector's own
generator (utils/netStat.family_episode_generator: every sub-step is a tc change, as exp.py applies it; the slot's @fast episode
length drawn at restart §2b's law, log-U(12, 48) s, its re-draws D/5-D/2 apart),
under the bulk load. Pure functions over the plan, the per-change snapshots and the per-second samples, so they test
without root.

Gated:
  - no class drops at change instants beyond the set loss: per change, the slot's netem drops from just before the tc
    commands to SETTLE_S after them, against the new state's loss over the same packets. Over the segment the observed
    drops must stay <= expected + 3 sqrt(expected) + 1 (a Poisson bound on the set loss);
  - the byte ratio (realised / set) in [RATIO_LO, RATIO_HI] over qualifying seconds that do not straddle a change: both
    bounding samples' class backlog above the state's netem delay line + 2 MTU (player_reading's rule) and no change
    of the slot between them; fewer than MIN_QUAL_S such seconds: "insufficient" (not a pass).
Reported: the drop share over the whole segment and the mean set loss; per change the excess; the changes per slot.
"""
from __future__ import annotations

import math
import random

SETTLE_S = 1.0
MTU = 1500
RATIO_LO, RATIO_HI = 0.9, 1.15
MIN_QUAL_S = 10


def schedule(platform: str, slot: str, seconds: float, seed: int = 0, family: str = "fast") -> list[dict]:
    """The slot's plan: [{t, dur, state, rate_kbit, delay_ms, loss_pct, ...}] covering `seconds` (seeded per slot; the
    global random state is restored). family "fast": the platform's @fast family (Q am. 17); "base": its base matrix's
    schedule (60 s episodes, the re-draw at 12-30 s), restart §2's walk segment. Both carry restart §2 as exp.py runs
    it: the walk's steps between re-draws and a delay / loss factor per slot (utils/netStat.session_shaping, seeded by
    the family, seed and slot). Needs networkSim on sys.path."""
    from utils.netStat import family_episode_generator, net_episode_generator, session_shaping
    from utils.shaping import (BASE_EPISODE_S, FAMILY_BASE, PLATFORM_MATRICES, draw_fast_episode_s,
                               initial_state_stationary, schedule_of)
    if family not in ("fast", "base"):
        raise ValueError(f"family {family!r}: fast or base")
    mid = f"{FAMILY_BASE[platform]}@fast" if family == "fast" else FAMILY_BASE[platform]
    transitions, cap, _ = PLATFORM_MATRICES[platform][mid]
    walk_rng, rec = session_shaping(f"dry-{family}:{seed}:{slot}")
    f = rec["dl_factor"]["value"]
    fe = None
    if family == "fast":
        # restart §2b (levi 00317: the dry run at the @fast law): the slot's episode length drawn as orchestrate draws a
        # session's, log-U(12, 48) s, keyed by the dry run's seed and slot; its re-draw at D/5-D/2
        fe = draw_fast_episode_s(f"dry-fast:{seed}:{slot}")["episode_s"]
        sc = schedule_of(mid, cap, episode_s=fe)
        gen = lambda: family_episode_generator(sc["episode_s"], transitions, sc["redraw_s"], sc["low_cap_s"],  # noqa: E731
                                               sc["self_loops"], initial_state_fn=initial_state_stationary,
                                               walk_rng=walk_rng, dl_factor=f)
    else:
        gen = lambda: net_episode_generator(BASE_EPISODE_S, transitions, max_consecutive_low=cap,  # noqa: E731
                                            initial_state_fn=initial_state_stationary, walk_rng=walk_rng, dl_factor=f)
    saved = random.getstate()
    random.seed(f"dry-{family}:{seed}:{slot}")
    plan, t = [], 0.0
    try:
        for ns in gen():
            if t >= seconds - 1e-6:
                break
            plan.append({"t": round(t, 3), "dur": round(ns.duration, 3), "state": ns.state, "rate_kbit": ns.rate,
                         "delay_ms": ns.delay_ms, "loss_pct": ns.loss_pct, "matrix_id": mid,
                         "redraw_kbit": ns.redraw_kbit, "walk_step": ns.walk_step, "dl_factor": f,
                         **({"fast_episode_s": fe} if fe is not None else {})})
            t += ns.duration
    finally:
        random.setstate(saved)
    return plan


def _counts(snap: dict | None) -> tuple[int, int] | None:
    n = (snap or {}).get("netem") or {}
    return (n["packets"], n["drops"]) if "packets" in n and "drops" in n else None


def change_drops(changes: list[dict]) -> dict:
    """changes: one slot's [{pre, post, settle, params}] (snapshots {t, netem: {packets, drops}}); a missing settle
    ends at the next change's pre."""
    rows, obs, exp = [], 0, 0.0
    for i, c in enumerate(changes):
        end = c.get("settle") or (changes[i + 1]["pre"] if i + 1 < len(changes) else None)
        a, b = _counts(c.get("pre")), _counts(end)
        if a is None or b is None:
            continue
        dp, dd = b[0] - a[0], b[1] - a[1]
        e = c["params"]["loss_pct"] / 100.0 * (dp + dd)
        obs, exp = obs + dd, exp + e
        rows.append({"t": c["pre"]["t"], "packets": dp, "drops": dd, "expected": round(e, 2), "excess": round(dd - e, 2)})
    bound = exp + 3 * math.sqrt(exp) + 1
    return {"changes_read": len(rows), "drops": obs, "expected_from_set_loss": round(exp, 2),
            "bound": round(bound, 2), "gate": "pass" if rows and obs <= bound else ("fail" if rows else "not read"),
            "max_excess": max((r["excess"] for r in rows), default=None), "per_change": rows}


def in_force(changes: list[dict], t: float) -> dict | None:
    """The params applied last at or before t (a change counts from its post snapshot)."""
    cur = None
    for c in changes:
        if c["post"]["t"] <= t:
            cur = c["params"]
    return cur


def ratio_non_straddling(samples: list[dict], dev: str, cls: str, changes: list[dict], t0: float, t1: float) -> dict:
    """The byte ratio over the qualifying seconds in [t0, t1] that contain no change of the slot (module docstring)."""
    rows = [x for x in samples if t0 <= x["t"] <= t1]
    inst = [(c["pre"]["t"], c["post"]["t"]) for c in changes]
    q = []
    for a, b in zip(rows, rows[1:]):
        if any(a["t"] <= hi and b["t"] >= lo for lo, hi in inst):
            continue
        p = in_force(changes, a["t"])
        if not p:
            continue
        thr = p["rate_kbit"] * 1000 / 8 * p["delay_ms"] / 1000 + 2 * MTU
        ca = ((a.get("classes") or {}).get(dev) or {}).get(cls) or {}
        cb = ((b.get("classes") or {}).get(dev) or {}).get(cls) or {}
        if ca.get("backlog", 0) > thr and cb.get("backlog", 0) > thr and "bytes" in ca and "bytes" in cb:
            q.append((b["t"] - a["t"], cb["bytes"] - ca["bytes"], p["rate_kbit"]))
    set_kbit_s = sum(d * r for d, _, r in q)
    ratio = (sum(x for _, x, _ in q) * 8 / 1000 / set_kbit_s) if set_kbit_s else None
    if len(q) < MIN_QUAL_S:
        gate = "insufficient"
    else:
        gate = "pass" if RATIO_LO <= ratio <= RATIO_HI else "fail"
    return {"qualifying_s": len(q), "ratio": round(ratio, 3) if ratio is not None else None, "gate": gate}


def whole_drop_share(changes: list[dict]) -> dict:
    """Over the segment: netem drops / (packets + drops) from the first change's pre to the last one's settle, and the
    mean set loss over the changes."""
    snaps = [s for c in changes for s in (c.get("pre"), c.get("settle")) if _counts(s)]
    if len(snaps) < 2:
        return {"drop_share": None, "set_loss_mean": None}
    a, b = _counts(snaps[0]), _counts(snaps[-1])
    dp, dd = b[0] - a[0], b[1] - a[1]
    return {"drop_share": round(dd / (dp + dd), 5) if dp + dd else None,
            "set_loss_mean": round(sum(c["params"]["loss_pct"] for c in changes) / len(changes) / 100, 5)}


NOTES = ("each change is the collector's: a sub-step of the platform's @fast family (or, in the walk segment, of its "
         "base matrix) or restart §2's walk step within one, applied with tc class / qdisc "
         "replace as single mode's make slow does; drops are netem's (emulated loss and tail drops alike), read from "
         "just before the tc commands to SETTLE_S after them; the ratio excludes every second containing a change")
