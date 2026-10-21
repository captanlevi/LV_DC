"""Label-diversity steering at the 10-22 tip (prereg d32a48ce, item 2): per-session dwell weights over the shaper's
states, drawn at SESSION START from the era's coverage deficit, never from the running session.

The deficit table (<trio root>/_summaries/coverage_joint.json, brad's era_report section, replaced by the post only in a
gap, atomically) holds per platform the counted seconds per class (tier classes and STALL), cumulative over counted
sessions, and per shaper state the seconds of each class delivered in it (by_state). From it:
  - deficit d(c) = max(0, target(c) - counted(c)) / target(c) per class (TARGETS_S: 2 h per tier the platform can
    deliver, 1 h STALL); a met target has d = 0 and pulls nothing;
  - a state's pull = sum over classes of P(c | state) * d(c), P from the table's by_state (a state the table has never
    seen gets the mean pull: neutral, never zero);
  - its weight w(state) = FLOOR + (1 - FLOOR) * pull / max pull, in [FLOOR, 1]: every state keeps >= FLOOR of its base
    weight, whatever the deficit; with no deficit (every target met, or no pull anywhere) every weight is 1 (the base);
  - the matrix's transitions into a state are multiplied by its weight and renormalised per row (self-loops included,
    so the dwell moves with the weight), and the start state is drawn from the steered matrix's stationary
    distribution with a generator seeded by the session's name.
Inputs: the table and the base matrix only (steer() takes nothing else; a test pins its signature and that this module
reads no player, page or session state). A missing / unreadable table -> the base matrix and the base start rule,
recorded. meta.json steering records the table's path, sha256, mtime, built_at, the deficits, the weights and the seed.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from pathlib import Path

FLOOR = 0.10                       # prereg d32a48ce: every state keeps >= 10 % of the base schedule's weight
TIER_TARGET_S = 2 * 3600.0         # >= 2 counted hours per tier the platform can deliver
STALL_TARGET_S = 1 * 3600.0        # >= 1 counted hour of STALL
TIERS = {"youtube": ["Q_144p", "Q_240p", "Q_360p", "Q_480p", "Q_720p", "Q_1080p"],
         "twitch": ["Q_144p", "Q_360p", "Q_480p", "Q_720p", "Q_1080p"],
         "tiktok": ["Q_360p", "Q_480p", "Q_720p", "Q_1080p"]}
TARGETS_S = {p: {**{t: TIER_TARGET_S for t in ts}, "STALL": STALL_TARGET_S} for p, ts in TIERS.items()}
TABLE_NAME = "coverage_joint.json"
RULE = ("prereg d32a48ce: session-start dwell weights from the last post's deficit table; w = FLOOR + (1 - FLOOR) * "
        "pull / max pull, pull = sum_c P(c | state) d(c); transitions into a state x w, rows renormalised; start state "
        "from the steered stationary distribution, seeded by the session name; no within-session input")


def load_table(path: str | os.PathLike | None) -> tuple[dict | None, dict]:
    """The table and its record {path, sha256, mtime, built_at, error}; (None, record) when absent or unreadable."""
    rec: dict = {"path": str(path) if path else None, "sha256": None, "mtime": None, "built_at": None, "error": None}
    if not path:
        rec["error"] = "no table path"
        return None, rec
    try:
        raw = Path(path).read_bytes()
        rec.update(sha256=hashlib.sha256(raw).hexdigest(), mtime=os.path.getmtime(path))
        table = json.loads(raw)
        if not isinstance(table, dict) or not isinstance(table.get("per_platform"), dict):
            raise ValueError("no per_platform")
        rec["built_at"] = table.get("built_at")
        return table, rec
    except (OSError, ValueError) as e:
        rec["error"] = f"{type(e).__name__}: {e}"[:200]
        return None, rec


def deficits(table: dict, platform: str) -> dict[str, float]:
    """d(c) in [0, 1] per class of the platform's targets; counted seconds from the table (absent = 0)."""
    pp = (table.get("per_platform") or {}).get(platform) or {}
    counted = {**(pp.get("tiers") or {}), "STALL": pp.get("stall_s") or 0.0}
    return {c: round(max(0.0, t - float(counted.get(c) or 0.0)) / t, 6) for c, t in TARGETS_S[platform].items()}


def state_weights(table: dict, platform: str, states: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    """(weights, pulls) per state: w = FLOOR + (1 - FLOOR) * pull / max pull; all 1 when nothing pulls."""
    d = deficits(table, platform)
    by_state = ((table.get("per_platform") or {}).get(platform) or {}).get("by_state") or {}
    pulls: dict[str, float | None] = {}
    for s in states:
        row = {c: float(v or 0.0) for c, v in (by_state.get(s) or {}).items() if c in d}
        tot = sum(row.values())
        pulls[s] = sum(v / tot * d[c] for c, v in row.items()) if tot > 0 else None
    seen = [p for p in pulls.values() if p is not None]
    mean = sum(seen) / len(seen) if seen else 0.0
    pulls = {s: (mean if p is None else p) for s, p in pulls.items()}
    top = max(pulls.values()) if pulls else 0.0
    if top <= 0:
        return {s: 1.0 for s in states}, pulls
    return {s: round(FLOOR + (1.0 - FLOOR) * p / top, 6) for s, p in pulls.items()}, pulls


def steer_matrix(transition: dict[str, dict[str, float]], w: dict[str, float]) -> dict[str, dict[str, float]]:
    """Transitions into a state x its weight, each row renormalised (a row whose mass vanishes keeps its base)."""
    out = {}
    for s, row in transition.items():
        new = {t: p * w.get(t, 1.0) for t, p in row.items()}
        tot = sum(new.values())
        out[s] = {t: v / tot for t, v in new.items()} if tot > 0 else dict(row)
    return out


def start_state(transition: dict[str, dict[str, float]], seed: str) -> str:
    """The start state from the (steered) matrix's stationary distribution, with a generator seeded by `seed`."""
    from utils.shaping import stationary_distribution
    pi = stationary_distribution(transition)
    states = sorted(pi, key=int)
    return random.Random(f"steering:{seed}").choices(states, weights=[pi[s] for s in states], k=1)[0]


def steer(platform: str, transition: dict[str, dict[str, float]], table_path: str | os.PathLike | None,
          seed: str) -> tuple[dict[str, dict[str, float]], str | None, dict]:
    """(steered matrix, start state, meta record) from the table at table_path and the base matrix only. A missing or
    unreadable table, or a platform without targets: (the base matrix, None = the base start rule, the record)."""
    table, trec = load_table(table_path)
    rec = {"rule": RULE, "floor": FLOOR, "targets_s": TARGETS_S.get(platform), "table": trec, "seed": seed,
           "applied": False}
    if table is None or platform not in TARGETS_S:
        rec["fallback"] = "base schedule" + ("" if table is not None else " (no table)")
        return transition, None, rec
    states = sorted(transition, key=int)
    w, pulls = state_weights(table, platform, states)
    steered = steer_matrix(transition, w)
    start = start_state(steered, seed)
    rec.update(applied=True, deficits=deficits(table, platform), pulls={s: round(p, 6) for s, p in pulls.items()},
               weights=w, start_state=start, table_list_sha=table.get("list_sha"), table_code=table.get("code"))
    return steered, start, rec
