#!/usr/bin/env python3
"""LVQoE prereg Addendum Q am. 17: each schedule family's state occupancy (its share of session time per shaper state),
from a seeded simulation of its exact schedule: utils/netStat.family_episode_generator with the family's episode length,
self-loop rule, re-draw and low-state cap in seconds, the base matrix's start rule (exp.py's) and the session's episodes
(coverage_report.SESSION_EPISODES scaled by shaping.session_episodes). Committed as fixtures/schedule_occupancy.json
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
                           stationary_distribution)

SIM_SEED = 0
SIM_SESSIONS = 2000
FIXTURE = HERE / "fixtures" / "schedule_occupancy.json"
OCCUPANCY_SHA256 = "3e5440b7d52c9ab64a650bf295c2c56577373262633c58ae95b0db82e73d0d80"


def families(platform: str) -> list[str]:
    return sorted(m for m in PLATFORM_MATRICES[platform] if "@" in m)


def simulate(platform: str, matrix_id: str, base_episodes: int, sessions: int = SIM_SESSIONS, seed: int = SIM_SEED) -> dict:
    """The family's occupancy over `sessions` simulated sessions (seeded per platform and id; the global random state
    is restored), its state changes per session (cuts at the cap included) and the schedule simulated."""
    transitions, cap, init = PLATFORM_MATRICES[platform][matrix_id]
    sc = schedule_of(matrix_id, cap)
    eps = session_episodes(base_episodes, matrix_id)
    pi = stationary_distribution(transitions)
    states = sorted(pi, key=int)

    def start(_td):                       # = shaping.initial_state_stationary, its distribution computed once
        return random.choices(states, weights=[pi[s] for s in states], k=1)[0]
    saved = random.getstate()
    random.seed(f"{seed}:{platform}:{matrix_id}")
    occ, changes = collections.Counter(), []
    try:
        for _ in range(sessions):
            prev, n = None, 0
            for ns in family_episode_generator(sc["episode_s"], transitions, sc["redraw_s"], sc["low_cap_s"],
                                               sc["self_loops"], max_episodes=eps,
                                               initial_state_fn=start if init == "stationary" else None):
                occ[ns.state] += ns.duration
                n += prev is not None and ns.state != prev
                prev = ns.state
            changes.append(n)
    finally:
        random.setstate(saved)
    tot = sum(occ.values())
    changes.sort()
    return {"occupancy": {s: round(occ[s] / tot, 6) for s in sorted(SCENARIOS, key=int)},
            "schedule": {**sc, "episodes": eps, "start_rule": init},
            "changes_per_session": {"mean": round(sum(changes) / len(changes), 2), "min": changes[0],
                                    "p10": changes[len(changes) // 10], "median": changes[len(changes) // 2],
                                    "p90": changes[len(changes) * 9 // 10], "max": changes[-1]},
            "sessions": sessions, "seed": f"{seed}:{platform}:{matrix_id}"}


def table(session_episodes_by_platform: dict[str, int]) -> dict:
    return {"rule": "LVQoE prereg Addendum Q am. 17: occupancy from a seeded simulation of the family's exact schedule",
            "schedules": {k: {**v, "redraw_s": list(v["redraw_s"])} for k, v in SCHEDULES.items()},
            "families": {p: {m: simulate(p, m, session_episodes_by_platform[p]) for m in families(p)}
                         for p in sorted(session_episodes_by_platform)}}


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
    import coverage_report
    text = dumps(table(coverage_report.SESSION_EPISODES))
    sha = hashlib.sha256(text.encode()).hexdigest()
    if a.write:
        FIXTURE.write_text(text)
        print(f"wrote {FIXTURE} sha256 {sha}")
    for p, fams in json.loads(text)["families"].items():
        for m, v in fams.items():
            print(p, m, v["schedule"]["episodes"], "episodes;", "changes", v["changes_per_session"], v["occupancy"])
    print("sha256", sha)
