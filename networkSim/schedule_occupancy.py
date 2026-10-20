#!/usr/bin/env python3
"""LVQoE prereg Addendum Q am. 17: each schedule family's state occupancy (its share of session time per shaper state),
from a seeded simulation of its exact schedule: utils/netStat.family_episode_generator with the family's episode length,
self-loop rule, re-draw and low-state cap in seconds, the base matrix's start rule (exp.py's) and the session's episodes.
Restart (prereg §4 / §2b): every simulated session draws its length (shaping.draw_session_minutes, U(10, 40) min) and,
on @fast, its episode length (shaping.draw_fast_episode_s, log-U(12, 48) s) as the collector does, keyed
occ:<seed>:<platform>:<id>:<i>; the occupancy is time-weighted over the sessions. Committed as fixtures/schedule_occupancy.json
with its sha256 (OCCUPANCY_SHA256, checked on every load). coverage_report qualifies a family for a tier class on this
occupancy, never on the base matrix's stationary mass (without self-loops it is not pi; the cap reshapes both).

    python3 schedule_occupancy.py            print each family's occupancy, changes per session and the table's sha
    python3 schedule_occupancy.py --write    rewrite the fixture (a change edits OCCUPANCY_SHA256 and the FROZEN log)
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

from utils.netStat import family_episode_generator  # noqa: E402
from utils.shaping import (PLATFORM_MATRICES, SCENARIOS, SCHEDULES, schedule_of, session_episodes,  # noqa: E402
                           stationary_distribution, draw_session_minutes, draw_fast_episode_s, SESSION_LEN_RULE,
                           FAST_EPISODE_RULE)

SIM_SEED = 0
SIM_SESSIONS = 2000
FIXTURE = HERE / "fixtures" / "schedule_occupancy.json"
OCCUPANCY_SHA256 = "cdf186fc02a1795a0f28d6dfc50cbb744d24428d9d42672af696f230dce312b6"


def families(platform: str) -> list[str]:
    return sorted(m for m in PLATFORM_MATRICES[platform] if "@" in m)


def _q(xs: list) -> dict:
    xs = sorted(xs)
    return {"mean": round(sum(xs) / len(xs), 2), "min": xs[0], "p10": xs[len(xs) // 10], "median": xs[len(xs) // 2],
            "p90": xs[len(xs) * 9 // 10], "max": xs[-1]}


def simulate(platform: str, matrix_id: str, sessions: int = SIM_SESSIONS, seed: int = SIM_SEED) -> dict:
    """The family's occupancy over `sessions` simulated sessions (seeded per platform and id; the global random state
    is restored), each of a drawn length (and, @fast, a drawn episode length), its state changes per session (cuts at
    the cap included) and the schedule simulated."""
    transitions, cap, init = PLATFORM_MATRICES[platform][matrix_id]
    fam = matrix_id.partition("@")[2]
    pi = stationary_distribution(transitions)
    states = sorted(pi, key=int)

    def start(_td):                       # = shaping.initial_state_stationary, its distribution computed once
        return random.choices(states, weights=[pi[s] for s in states], k=1)[0]
    saved = random.getstate()
    random.seed(f"{seed}:{platform}:{matrix_id}")
    occ, changes, eps_all, mins, ep_s = collections.Counter(), [], [], [], []
    try:
        for i in range(sessions):
            key = f"occ:{seed}:{platform}:{matrix_id}:{i}"
            m = draw_session_minutes(key)["minutes"]
            fe = draw_fast_episode_s(key)["episode_s"] if fam == "fast" else None
            sc = schedule_of(matrix_id, cap, m, episode_s=fe)
            eps = session_episodes(m, matrix_id, fe)
            prev, n = None, 0
            for ns in family_episode_generator(sc["episode_s"], transitions, sc["redraw_s"], sc["low_cap_s"],
                                               sc["self_loops"], max_episodes=eps,
                                               initial_state_fn=start if init == "stationary" else None):
                occ[ns.state] += ns.duration
                n += prev is not None and ns.state != prev
                prev = ns.state
            changes.append(n)
            eps_all.append(eps)
            mins.append(m)
            ep_s.append(sc["episode_s"])
    finally:
        random.setstate(saved)
    tot = sum(occ.values())
    sc = schedule_of(matrix_id, cap, 25.0)
    return {"occupancy": {s: round(occ[s] / tot, 6) for s in sorted(SCENARIOS, key=int)},
            "schedule": {k: v for k, v in sc.items() if k not in ("episode_s", "redraw_s")} | {
                "start_rule": init, "session_minutes_rule": SESSION_LEN_RULE,
                "episode_s_rule": FAST_EPISODE_RULE if fam == "fast" else "the session's time / 3",
                "redraw_rule": "the base's (12-30 s)" if fam == "slow" else "D/5-D/2"},
            "episodes_per_session": _q(eps_all), "session_minutes": _q([round(x, 2) for x in mins]),
            "episode_s": _q([round(x, 2) for x in ep_s]),
            "changes_per_session": _q(changes), "sessions": sessions, "seed": f"{seed}:{platform}:{matrix_id}"}


def table() -> dict:
    return {"rule": "LVQoE prereg Addendum Q am. 17: occupancy from a seeded simulation of the family's exact schedule; "
                    "restart §4 / §2b: drawn session and @fast episode lengths",
            "schedules": {k: {**v, "redraw_s": list(v["redraw_s"])} for k, v in SCHEDULES.items()},
            "families": {p: {m: simulate(p, m) for m in families(p)} for p in sorted(PLATFORM_MATRICES)}}


def dumps(t: dict) -> str:
    return json.dumps(t, indent=1, sort_keys=True) + "\n"


def load() -> dict:
    """The committed table; refuses one whose sha256 is not OCCUPANCY_SHA256."""
    raw = FIXTURE.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != OCCUPANCY_SHA256:
        raise RuntimeError(f"{FIXTURE.name}: sha256 {sha[:12]} is not the committed {OCCUPANCY_SHA256[:12]} (am. 17)")
    return json.loads(raw)


def occupancy(platform: str, matrix_id: str) -> dict[str, float]:
    return load()["families"][platform][matrix_id]["occupancy"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    text = dumps(table())
    sha = hashlib.sha256(text.encode()).hexdigest()
    if a.write:
        FIXTURE.write_text(text)
        print(f"wrote {FIXTURE} sha256 {sha}")
    for p, fams in json.loads(text)["families"].items():
        for m, v in fams.items():
            print(p, m, "episodes", v["episodes_per_session"], "changes", v["changes_per_session"], v["occupancy"])
    print("sha256", sha)
