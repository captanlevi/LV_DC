#!/usr/bin/env python3
"""Restart prereg 2026-10-21 §2: a seeded simulation of the shaper with the walk and the per-session delay / loss factor,
for the design check. Per platform x matrix id (every utils/shaping.PLATFORM_MATRICES id: base matrices on
utils/netStat.net_episode_generator, the "@slow" / "@fast" families on family_episode_generator, each with exp.py's
start rule and low-state cap) at the fixed coverage_report.SESSION_EPISODES lengths (the restart's per-session length
draw is not simulated: occupancy and changes per minute do not depend on it beyond the episode count):
  - occupancy: the share of session time per state, with the walk (the walk draws from its own generator, so the
    state sequence and every sub-step's duration are the ones without it: test_shaping_walk);
  - tc changes per session and per minute, before the walk (the re-draws, walk_step 0) and after (every change);
  - the walk (linear rate): steps per sub-step, the span of the rate over a sub-step as a share of the state's band
    (sub-steps with >= 1 step, and those with exactly WALK_REF_STEPS steps, 21-23 s), the realised over the re-drawn
    rate (time-weighted mean per state; the walk's stationary law is the re-draw's, uniform on the band, so about 1),
    and the realised and re-drawn rate over the state's band midpoint (time-weighted p5 / p50 / p95);
plus, per state, the realised delay and loss against the re-draw under the factor (DL_SAMPLES draws).

    python3 walk_simulation.py            print the table and its sha256
    python3 walk_simulation.py --write    rewrite fixtures/walk_simulation.json (WALK_SIM_SHA256 is its sha256)
    python3 walk_simulation.py --calibrate    utils/netStat.WALK_SIGMA_PER_BAND from calibrate_sigma
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from utils import netStat as NS  # noqa: E402
from utils.shaping import (BASE_EPISODE_S, PLATFORM_MATRICES, SCENARIOS, schedule_of, session_episodes,  # noqa: E402
                           stationary_distribution)

SIM_SEED = 0
SIM_SESSIONS = 1000
DL_SAMPLES = 20000
REF_SUBSTEP_S = (21.0, 23.0)       # [lo, hi): the sub-steps with exactly WALK_REF_STEPS steps
CALIB_SUBSTEPS = 20000            # per state
FIXTURE = HERE / "fixtures" / "walk_simulation.json"
WALK_SIM_SHA256 = "20c6ebccaf1ef10fdbf7249208099a16c0c8faedfc337537ebbbb0e741df29ac"


def _q(xs: list[float], ps=(0.1, 0.5, 0.9)) -> dict:
    if not xs:
        return {}
    xs = sorted(xs)
    return {f"p{int(p * 100)}": round(xs[min(len(xs) - 1, int(p * len(xs)))], 4) for p in ps}


def _wq(pairs: list[tuple[float, float]], ps=(0.05, 0.5, 0.95)) -> dict:
    """Time-weighted quantiles of (value, weight) pairs."""
    pairs = sorted(p for p in pairs if p[1] > 0)
    tot = sum(w for _, w in pairs)
    out, acc, i = {}, 0.0, 0
    for p in ps:
        while i < len(pairs) - 1 and acc + pairs[i][1] < p * tot:
            acc += pairs[i][1]
            i += 1
        out[f"p{int(p * 100)}"] = round(pairs[i][0], 4)
    return out


def _stats(xs: list[int]) -> dict:
    xs = sorted(xs)
    n = len(xs)
    return {"mean": round(sum(xs) / n, 2), "min": xs[0], "p10": xs[n // 10], "median": xs[n // 2],
            "p90": xs[n * 9 // 10], "max": xs[-1]}


def generator(platform: str, matrix_id: str, base_episodes: int, walk_rng, dl_factor: float, start_fn):
    """exp.py's generator for the id (base: 60 s episodes, the matrix's cap in episodes; family: its schedule)."""
    transitions, cap, _ = PLATFORM_MATRICES[platform][matrix_id]
    sc = schedule_of(matrix_id, cap, base_episodes)
    eps = session_episodes(base_episodes, matrix_id)
    if sc["schedule"] == "base":
        return NS.net_episode_generator(BASE_EPISODE_S, transitions, max_episodes=eps, max_consecutive_low=cap,
                                        initial_state_fn=start_fn, walk_rng=walk_rng, dl_factor=dl_factor), sc, eps
    return NS.family_episode_generator(sc["episode_s"], transitions, tuple(sc["redraw_s"]), sc["low_cap_s"],
                                       sc["self_loops"], max_episodes=eps, initial_state_fn=start_fn,
                                       walk_rng=walk_rng, dl_factor=dl_factor), sc, eps


def simulate(platform: str, matrix_id: str, base_episodes: int, sessions: int = SIM_SESSIONS, seed: int = SIM_SEED) -> dict:
    transitions, cap, init = PLATFORM_MATRICES[platform][matrix_id]
    pi = stationary_distribution(transitions)
    states = sorted(pi, key=int)

    def stationary(_td):                  # = shaping.initial_state_stationary, its distribution computed once
        return random.choices(states, weights=[pi[s] for s in states], k=1)[0]
    start_fn = stationary if init == "stationary" else None
    saved = random.getstate()
    random.seed(f"{seed}:{platform}:{matrix_id}")
    occ = collections.Counter()
    before, after, steps_per_sub, span, span_ref = [], [], [], [], []
    real_t, redraw_t, dur_t = collections.Counter(), collections.Counter(), collections.Counter()
    subs_n = collections.Counter()        # levi (brad's walk parity): each state's support behind its ratio
    real_mid, redraw_mid = [], []
    session_s = None

    def close(sub: list) -> None:         # one sub-step's rows: its steps and its rate span over the band
        if not sub:
            return
        st = sub[0].state
        subs_n[st] += 1
        lw = SCENARIOS[st].rate_max - SCENARIOS[st].rate_min
        xs = [r.rate for r in sub]
        steps_per_sub.append(len(sub) - 1)
        if len(sub) > 1:
            span.append((max(xs) - min(xs)) / lw)
            if REF_SUBSTEP_S[0] <= sum(r.duration for r in sub) < REF_SUBSTEP_S[1]:
                span_ref.append((max(xs) - min(xs)) / lw)
    try:
        for i in range(sessions):
            walk_rng, rec = NS.session_shaping(f"sim:{seed}:{platform}:{matrix_id}:{i}")
            gen, sc, eps = generator(platform, matrix_id, base_episodes, walk_rng, rec["dl_factor"]["value"], start_fn)
            session_s = sc["episode_s"] * eps
            n_before = n_after = 0
            sub: list = []
            for ns in gen:
                n_after += 1
                if ns.walk_step == 0:
                    n_before += 1
                    close(sub)
                    sub = []
                sub.append(ns)
                occ[ns.state] += ns.duration
                real_t[ns.state] += ns.rate * ns.duration
                redraw_t[ns.state] += ns.redraw_kbit * ns.duration
                dur_t[ns.state] += ns.duration
                mid = (SCENARIOS[ns.state].rate_min + SCENARIOS[ns.state].rate_max) / 2
                real_mid.append((ns.rate / mid, ns.duration))
                redraw_mid.append((ns.redraw_kbit / mid, ns.duration))
            close(sub)
            before.append(n_before)
            after.append(n_after)
    finally:
        random.setstate(saved)
    tot = sum(occ.values())
    minutes = session_s / 60.0
    return {"schedule": {**sc, "episodes": eps, "start_rule": init, "session_s": session_s},
            "occupancy": {s: round(occ[s] / tot, 6) for s in sorted(SCENARIOS, key=int)},
            "changes_per_session": {"before_walk": _stats(before), "after_walk": _stats(after)},
            "changes_per_min": {"before_walk": round(sum(before) / len(before) / minutes, 2),
                                "after_walk": round(sum(after) / len(after) / minutes, 2)},
            "walk": {"steps_per_substep": {"mean": round(sum(steps_per_sub) / len(steps_per_sub), 2),
                                           **_q(steps_per_sub)},
                     "span_share": {"substeps": len(span), "mean": round(sum(span) / len(span), 4) if span else None,
                                    **_q(span)},
                     "span_share_ref_length": {"substeps": len(span_ref),
                                               "mean": round(sum(span_ref) / len(span_ref), 4) if span_ref else None,
                                               **_q(span_ref)},
                     "realised_over_redraw_by_state": {s: round(real_t[s] / redraw_t[s], 4)
                                                       for s in sorted(dur_t, key=int)},
                     "support_by_state": {s: {"substeps": subs_n[s], "seconds": round(dur_t[s], 1)}
                                          for s in sorted(dur_t, key=int)},
                     "realised_over_midpoint": _wq(real_mid), "redraw_over_midpoint": _wq(redraw_mid)},
            "sessions": sessions, "seed": f"{seed}:{platform}:{matrix_id}"}


def reference_span(c: float, n: int = CALIB_SUBSTEPS, seed: int = SIM_SEED) -> float:
    """The walk's mean span (share of the band) over the reference sub-step at sigma = c x the band: WALK_REF_STEPS
    reflected steps (utils/netStat.reflect) from the state's re-draw (randint on the band), n sub-steps per state,
    states weighted equally."""
    tot = 0.0
    for st in sorted(SCENARIOS, key=int):
        rng = random.Random(f"calib:{seed}:{st}")
        sc = SCENARIOS[st]
        lo, hi = sc.rate_min, sc.rate_max
        for _ in range(n):
            x = float(rng.randint(lo, hi))
            mn = mx = x
            for _ in range(NS.WALK_REF_STEPS):
                x = NS.reflect(x + rng.gauss(0.0, c * (hi - lo)), lo, hi)
                mn, mx = min(mn, x), max(mx, x)
            tot += (mx - mn) / (hi - lo)
    return tot / (n * len(SCENARIOS))


def calibrate_sigma(target: float = NS.WALK_SPAN_SHARE, lo: float = 0.10, hi: float = 0.25, iters: int = 14) -> float:
    """WALK_SIGMA_PER_BAND: the c whose reference_span is target (bisection), to 3 decimals."""
    for _ in range(iters):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if reference_span(mid) < target else (lo, mid)
    return round((lo + hi) / 2, 3)


def delay_loss_by_state(n: int = DL_SAMPLES, seed: int = SIM_SEED) -> dict:
    """Per state: the re-drawn and the realised delay / loss (x a factor drawn per sample as a session's), p5 / p50 / p95."""
    rng = random.Random(f"dl-table:{seed}")
    out = {}
    for s in sorted(SCENARIOS, key=int):
        sc = SCENARIOS[s]
        rd, rl, fd, fl = [], [], [], []
        for _ in range(n):
            d, l, f = rng.randint(sc.delay_min, sc.delay_max), round(rng.uniform(sc.loss_pct_min, sc.loss_pct_max), 1), \
                NS.draw_dl_factor(rng)
            rd.append((d, 1.0))
            rl.append((l, 1.0))
            fd.append((int(round(d * f)), 1.0))
            fl.append((round(l * f, 3), 1.0))
        out[s] = {"delay_ms": {"redraw": _wq(rd), "realised": _wq(fd)},
                  "loss_pct": {"redraw": _wq(rl), "realised": _wq(fl)}}
    return out


def table(session_episodes_by_platform: dict[str, int]) -> dict:
    return {"rule": "restart prereg 2026-10-21 §2: the shaper with the walk and the delay / loss factor, seeded",
            "walk": {"rule": NS.WALK_RULE, "step_s": NS.WALK_STEP_S, "last_min_s": NS.WALK_LAST_MIN_S,
                     "span_share": NS.WALK_SPAN_SHARE, "ref_steps": NS.WALK_REF_STEPS,
                     "space": "linear", "sigma_per_band": NS.WALK_SIGMA_PER_BAND,
                     "reference_span_at_sigma": round(reference_span(NS.WALK_SIGMA_PER_BAND), 4),
                     "sigma_kbit_by_state": {s: round(NS.walk_sigma(s), 3) for s in sorted(SCENARIOS, key=int)}},
            "dl_factor": {"rule": NS.DL_FACTOR_RULE, "lo": NS.DL_FACTOR_LO, "hi": NS.DL_FACTOR_HI},
            "session_episodes": session_episodes_by_platform,
            "session_length_note": "fixed SESSION_EPISODES lengths; the restart's per-session length draw is not simulated",
            "matrices": {p: {m: simulate(p, m, session_episodes_by_platform[p]) for m in sorted(PLATFORM_MATRICES[p])}
                         for p in sorted(session_episodes_by_platform)},
            "delay_loss_by_state": delay_loss_by_state()}


def dumps(t: dict) -> str:
    return json.dumps(t, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    a = ap.parse_args()
    if a.calibrate:
        c = calibrate_sigma()
        print(f"sigma per band {c} (committed {NS.WALK_SIGMA_PER_BAND}): reference span {reference_span(c):.4f}")
        sys.exit(0)
    import coverage_report
    text = dumps(table(coverage_report.SESSION_EPISODES))
    sha = hashlib.sha256(text.encode()).hexdigest()
    if a.write:
        FIXTURE.write_text(text)
        print(f"wrote {FIXTURE} sha256 {sha}")
    for p, ms in json.loads(text)["matrices"].items():
        for m, v in ms.items():
            w = v["walk"]
            print(p, m, "changes/min", v["changes_per_min"], "span", w["span_share"].get("mean"),
                  "ref", w["span_share_ref_length"].get("mean"), "real/mid", w["realised_over_midpoint"])
    print("sha256", sha)
