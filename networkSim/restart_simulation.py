#!/usr/bin/env python3
"""The restart's round design simulated (rushi via the supervisor 2026-10-20: commit the N-draw simulation so brad can
design-check it). Windows as orchestrate --n-draw --window-min runs them (restart prereg §1 / §4 / §10): each window
draws N and its composition (n_draw.draw_at, the record sequence of record.ERA, the per-N rotation counters); each slot,
started STAGGER_S after the previous one, draws its next session's length (shaping.draw_session_minutes, keyed as
_trio_refill keys it: <run>:<tag>:<index>) and runs it when it fits before the window's end, else idles to the end (no
truncation, no re-draw); a refill costs REFILL_GAP_S; a window costs its minutes plus OVERHEAD_S. Deterministic (seeded
draws; synthetic run ids SIM<k>). Stated assumptions: STAGGER_S (= orchestrate.TRIO_STAGGER_S), REFILL_GAP_S and
OVERHEAD_S. Output: fixtures/restart_simulation.json (every window and session, and a summary: windows, N counts, the
rotations, sessions / minutes per platform, the collected length's terciles against the drawn law, slot busy share,
per-session mean concurrency).

    python3 restart_simulation.py [--hours 24] [--window-min 120] [--write]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n_draw as N  # noqa: E402
import record  # noqa: E402
from utils.shaping import draw_session_minutes, SESSION_MINUTES  # noqa: E402

FIXTURE = HERE / "fixtures" / "restart_simulation.json"
STAGGER_S = 30.0          # = orchestrate.TRIO_STAGGER_S (tested)
REFILL_GAP_S = 60.0       # stated assumption: a slot's teardown, pick and next start
OVERHEAD_S = 300.0        # stated assumption: a window's tree / captures / merge / next window's start
WINDOW_MIN = 120.0        # restart prereg §10 (levi)
TAG_PREFIX = {"youtube": "yt", "twitch": "tw", "tiktok": "tt"}


def tags_of(comp: dict[str, int]) -> list[tuple[str, str]]:
    """(tag, platform) per slot, as trio_slots.build_slots names them (yt1, yt2, tw1 ...), in start order."""
    return [(f"{TAG_PREFIX[p]}{i}", p) for p in ("youtube", "twitch", "tiktok") for i in range(1, comp.get(p, 0) + 1)]


def _concurrency(spans: list[tuple[float, float]], i: int) -> float:
    """Session i's time-weighted mean count of running sessions (itself included), as trio_slots.concurrency_over."""
    a, b = spans[i]
    tot = sum(max(0.0, min(b, d) - max(a, c)) for j, (c, d) in enumerate(spans) if j != i)
    return 1.0 + tot / (b - a)


def simulate(hours: float = 24.0, window_min: float = WINDOW_MIN, era: str = record.ERA) -> dict:
    t, windows, idx = 0.0, [], {n: 0 for n in N.ROTATING}
    W = window_min * 60
    while t < hours * 3600:
        k = len(windows)
        d = N.draw_at(k, idx, era, True)
        if d["n"] in idx:
            idx[d["n"]] += 1
        sessions, spans = [], []
        for i, (tag, p) in enumerate(tags_of(d["composition"])):
            ts, j = i * STAGGER_S, 0
            while True:
                j += 1
                m = draw_session_minutes(f"SIM{k}:{tag}:{j}")["minutes"]
                if ts + m * 60 > W:                  # does not fit: the slot idles to the window's end
                    break
                sessions.append({"tag": tag, "platform": p, "start_s": ts, "minutes": m})
                spans.append((ts, ts + m * 60))
                ts += m * 60 + REFILL_GAP_S
        for i, s in enumerate(sessions):
            s["concurrency_mean"] = round(_concurrency(spans, i), 3)
        dur = W + OVERHEAD_S
        busy = sum(s["minutes"] * 60 for s in sessions)
        windows.append({"window": k, "t0_h": round(t / 3600, 3), "n": d["n"], "composition": d["composition"],
                        **({"rotation": d["rotation"]} if "rotation" in d else {}), "u": d["u"],
                        "slot_busy_share": round(busy / (d["n"] * dur), 4), "sessions": sessions})
        t += dur
    ss = [s for w in windows for s in w["sessions"]]
    lo, hi = SESSION_MINUTES
    cuts = [lo + (hi - lo) * j / 3 for j in (1, 2)]
    lens = [s["minutes"] for s in ss]
    mins_p = Counter()
    for s in ss:
        mins_p[s["platform"]] += s["minutes"]
    conc = sorted(s["concurrency_mean"] for s in ss)
    summary = {
        "hours": hours, "window_min": window_min, "stagger_s": STAGGER_S, "refill_gap_s": REFILL_GAP_S,
        "overhead_s": OVERHEAD_S, "era": era, "windows": len(windows),
        "n_counts": {str(n): c for n, c in sorted(Counter(w["n"] for w in windows).items())},
        "rotations": {str(n): [list(w["rotation"].values())[1] for w in windows if w["n"] == n] for n in N.ROTATING},
        "sessions": len(ss), "sessions_per_platform": dict(sorted(Counter(s["platform"] for s in ss).items())),
        "session_minutes_per_platform": {p: round(v, 1) for p, v in sorted(mins_p.items())},
        "length_mean_min": round(sum(lens) / len(lens), 2), "length_mean_drawn_law_min": (lo + hi) / 2,
        "length_tercile_cuts_min": cuts,
        "length_tercile_shares": [round(sum(1 for x in lens if a <= x < b) / len(lens), 3)
                                  for a, b in ((lo, cuts[0]), (cuts[0], cuts[1]), (cuts[1], hi + 1))],
        "slot_busy_share_mean": round(sum(w["slot_busy_share"] for w in windows) / len(windows), 4),
        "concurrency_mean_p10_p50_p90": [conc[len(conc) // 10], conc[len(conc) // 2], conc[len(conc) * 9 // 10]],
    }
    return {"rule": {"n_draw": N.N_DRAW_RULE, "session_length": draw_session_minutes("x")["rule"],
                     "window": "restart §10: a slot runs its next drawn length when it fits before the window's end, "
                               "else idles to the end (no truncation, no re-draw)"},
            "summary": summary, "windows": windows}


def dumps(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--window-min", type=float, default=WINDOW_MIN)
    ap.add_argument("--write", action="store_true", help="write fixtures/restart_simulation.json (the defaults only)")
    a = ap.parse_args(argv)
    out = simulate(a.hours, a.window_min)
    text = dumps(out)
    if a.write:
        if (a.hours, a.window_min) != (24.0, WINDOW_MIN):
            sys.exit("--write takes the defaults only (the committed fixture)")
        FIXTURE.write_text(text)
    print(json.dumps(out["summary"], indent=1))
    print(f"sha256 {hashlib.sha256(text.encode()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
