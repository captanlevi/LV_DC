#!/usr/bin/env python3
"""Random N per round (LVQoE restart_2026-10-21_prereg_levi.md §1, rushi GO 2026-10-20): each round's N is drawn from
{3, 4, 5} with weights 0.2 / 0.3 / 0.5 by a seeded generator, independent of coverage, time and anything observed; the
composition is fixed per N (3 = 1 / 1 / 1; 4 = 2 / 1 / 1, the doubled platform rotating yt -> tw -> tt across the N = 4
rounds; 5 = 2 / 2 / 1). An N that fails its scaling gate leaves the support (--n-support) and the weights renormalise.

A round is one orchestrate --concurrent run. Its index k is the number of earlier draws of the same era and record
flag in the trio root's slots log (trio_up events carrying n_draw), so a run that ends before its trio_up re-draws
the same N, and a pilot never moves the record's sequence. The draw for round k is random.Random("<seed>:<era>:
<record|pilot>:<k>").random() against the cumulative renormalised weights; the N = 4 rotation index is the count of
earlier N = 4 draws in that sequence. The draw is recorded in trio_up (n_draw) and in every session's meta.json
concurrency.n_draw.

    python3 n_draw.py --simulate 200 [--support 3,4,5]     (a simulated sequence of rounds: the draws and rotation)
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

N_SUPPORT = (3, 4, 5)
N_WEIGHTS = {3: 0.2, 4: 0.3, 5: 0.5}
N_DRAW_SEED = 0
N4_ROTATION = ("youtube", "twitch", "tiktok")
COMPOSITIONS = {3: {"youtube": 1, "twitch": 1, "tiktok": 1}, 5: {"youtube": 2, "twitch": 2, "tiktok": 1}}
N_DRAW_RULE = ("restart prereg §1: N per round from {3, 4, 5}, weights 0.2 / 0.3 / 0.5 renormalised over the support; "
               "round k = the earlier draws of this era and record flag in the slots log; u = random.Random("
               "'<seed>:<era>:<record|pilot>:<k>').random() against the cumulative weights; 3 = 1/1/1, 5 = 2/2/1, "
               "4 = 2/1/1 with the doubled platform yt -> tw -> tt by the count of earlier N = 4 draws")


def parse_support(spec: str | None) -> tuple[int, ...]:
    """'3,4,5' -> (3, 4, 5); a subset of N_SUPPORT, at least one."""
    if not spec:
        return N_SUPPORT
    try:
        out = tuple(sorted({int(x) for x in spec.split(",") if x.strip()}))
    except ValueError:
        raise ValueError(f"--n-support {spec!r}: integers from {N_SUPPORT}")
    if not out or set(out) - set(N_SUPPORT):
        raise ValueError(f"--n-support {spec!r}: a non-empty subset of {N_SUPPORT}")
    return out


def weights_of(support: tuple[int, ...]) -> dict[int, float]:
    """The weights renormalised over the support (an N that failed its scaling gate is out)."""
    tot = sum(N_WEIGHTS[n] for n in support)
    return {n: N_WEIGHTS[n] / tot for n in support}


def composition(n: int, n4_index: int = 0) -> dict[str, int]:
    """The fixed composition of N (4: the doubled platform by the rotation index)."""
    if n == 4:
        doubled = N4_ROTATION[n4_index % len(N4_ROTATION)]
        return {p: 2 if p == doubled else 1 for p in N4_ROTATION}
    return dict(COMPOSITIONS[n])


def prior_draws(slots_log: Path | None, era: str, record: bool) -> list[dict]:
    """The earlier draws of this era and record flag (trio_up events carrying n_draw), in log order."""
    out = []
    try:
        lines = Path(slots_log).read_text().splitlines() if slots_log else []
    except OSError:
        lines = []
    for ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        d = ev.get("n_draw") if ev.get("event") == "trio_up" else None
        if isinstance(d, dict) and d.get("era") == era and bool(d.get("record")) == bool(record):
            out.append(d)
    return out


def draw_at(k: int, n4_index: int, era: str, record: bool, support: tuple[int, ...] = N_SUPPORT,
            seed: int = N_DRAW_SEED) -> dict:
    """Round k's draw, given the count of earlier N = 4 draws in the sequence."""
    w = weights_of(support)
    u = random.Random(f"{seed}:{era}:{'record' if record else 'pilot'}:{k}").random()
    acc, n = 0.0, support[-1]
    for m in support:
        acc += w[m]
        if u < acc:
            n = m
            break
    comp = composition(n, n4_index)
    return {"rule": N_DRAW_RULE, "seed": seed, "era": era, "record": bool(record), "round": k, "u": round(u, 6),
            "support": list(support), "weights": {str(m): round(v, 4) for m, v in w.items()}, "n": n,
            "composition": comp, **({"n4_index": n4_index, "doubled": N4_ROTATION[n4_index % 3]} if n == 4 else {})}


def draw(slots_log: Path | None, era: str, record: bool, support: tuple[int, ...] = N_SUPPORT,
         seed: int = N_DRAW_SEED) -> dict:
    """This round's draw: k and the N = 4 rotation index from the slots log's earlier draws."""
    prior = prior_draws(slots_log, era, record)
    return draw_at(len(prior), sum(1 for d in prior if d.get("n") == 4), era, record, support, seed)


def simulate(rounds: int, era: str, record: bool = True, support: tuple[int, ...] = N_SUPPORT,
             seed: int = N_DRAW_SEED) -> list[dict]:
    """A simulated sequence of rounds, each appended to the log as run_trio does (the draws and the rotation)."""
    out, n4 = [], 0
    for k in range(rounds):
        d = draw_at(k, n4, era, record, support, seed)
        n4 += d["n"] == 4
        out.append(d)
    return out


def main(argv=None) -> int:
    import record as R
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simulate", type=int, default=50, help="rounds to simulate (default 50)")
    ap.add_argument("--support", default=None, help="the support, e.g. 3,4,5 (default)")
    ap.add_argument("--era", default=R.ERA)
    ap.add_argument("--json", action="store_true", help="every draw as JSON lines")
    a = ap.parse_args(argv)
    seq = simulate(a.simulate, a.era, True, parse_support(a.support))
    if a.json:
        for d in seq:
            print(json.dumps({k: v for k, v in d.items() if k != "rule"}))
        return 0
    from collections import Counter
    ns = Counter(d["n"] for d in seq)
    dbl = Counter(d["doubled"] for d in seq if d["n"] == 4)
    print(f"{a.simulate} rounds, era {a.era}, support {list(parse_support(a.support))}: "
          f"N {dict(sorted(ns.items()))}; N = 4 doubled {dict(dbl)}")
    print(" ".join(f"{d['n']}{d['doubled'][:2] if d['n'] == 4 else ''}" for d in seq))
    return 0


if __name__ == "__main__":
    sys.exit(main())
