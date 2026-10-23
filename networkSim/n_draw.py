#!/usr/bin/env python3
"""Random N per round (LVQoE restart_2026-10-21_prereg_levi.md §1, rushi GO 2026-10-20): each round's N is drawn from
{3, 4, 5} with weights 0.2 / 0.3 / 0.5 by a seeded generator, independent of coverage, time and anything observed; the
composition is fixed per N (3 = 1 / 1 / 1; 4 = 2 / 1 / 1, the doubled platform rotating yt -> tw -> tt across the N = 4
rounds; 5: the single platform rotating yt -> tw -> tt across the N = 5 rounds, 1 / 2 / 2, 2 / 1 / 2, 2 / 2 / 1; levi on
brad's design check 00231). An N that fails its scaling gate leaves the support (--n-support) and the weights
renormalise; a composition that fails its decode stress leaves its rotation (--n-exclude 4:tiktok,5:youtube: N = 4's
doubled / N = 5's single platform), stated in every draw, and an N with no composition left leaves the support.

A round is one orchestrate --concurrent run. Its index k is the number of earlier draws of the same era and record
flag in the trio root's slots log (trio_up events carrying n_draw; outside the T* era only runs that started a session,
levi 18:58), so a run that ends before its trio_up, or a void run that started nothing, re-draws the same N, and a pilot never moves the record's sequence. The draw for round k is random.Random("<seed>:<era>:
<record|pilot>:<k>").random() against the cumulative renormalised weights; an N's rotation index is the count of
earlier draws of that N in the sequence (the counters persist per era in the slots log, never per process or day). The
draw is recorded in trio_up (n_draw) and in every session's meta.json concurrency.n_draw. A window (orchestrate
--window-min) is a round.

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
ROTATION = ("youtube", "twitch", "tiktok")      # N = 4's doubled and N = 5's single platform, in this order
ROTATING = (4, 5)
N_DRAW_RULE = ("restart prereg §1: N per round from {3, 4, 5}, weights 0.2 / 0.3 / 0.5 renormalised over the support "
               "(an N with no composition left is out); round k = the earlier draws of this era and record flag in the "
               "slots log (a record draw fails closed on an unreadable log); u = random.Random('<seed>:<era>:"
               "<record|pilot>:<k>').random() against the cumulative weights; 3 = 1/1/1; 4: the doubled platform "
               "rotating yt -> tw -> tt (2/1/1, 1/2/1, 1/1/2); 5: the single platform rotating yt -> tw -> tt (1/2/2, "
               "2/1/2, 2/2/1); less any excluded composition, by the count of earlier draws of that N; a record draw's "
               "support / exclusions are the era's last record draw's unless a signed change narrows them")


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


def parse_exclude(spec: str | None) -> tuple[tuple[int, str], ...]:
    """'4:tiktok,5:youtube' -> ((4, 'tiktok'), (5, 'youtube')): compositions out of their rotation (N = 4's doubled,
    N = 5's single platform)."""
    out = set()
    for x in (spec or "").split(","):
        if not x.strip():
            continue
        n, _, p = x.strip().partition(":")
        if not n.isdigit() or int(n) not in ROTATING or p not in ROTATION:
            raise ValueError(f"--n-exclude {x!r}: <4|5>:<{'|'.join(ROTATION)}>")
        out.add((int(n), p))
    return tuple(sorted(out))


def rotation(n: int, exclude: tuple = ()) -> tuple[str, ...]:
    """N's rotation (the rotating platform's order), less the excluded compositions; () for N = 3."""
    return tuple(p for p in ROTATION if (n, p) not in exclude) if n in ROTATING else ()


def composition(n: int, index: int = 0, exclude: tuple = ()) -> dict[str, int]:
    """The fixed composition of N: 3 = 1/1/1; 4 the rotating platform doubled; 5 the rotating platform single."""
    if n == 3:
        return {p: 1 for p in ROTATION}
    rot = rotation(n, exclude)
    if not rot:
        raise ValueError(f"N = {n}: every composition excluded")
    r = rot[index % len(rot)]
    return {p: (2 if p == r else 1) if n == 4 else (1 if p == r else 2) for p in ROTATION}


# levi 2026-10-23 18:58 (collector v3): a void run (trio_up, no session started; the w11 07:54 data-root drain) is not a draw,
# so the relaunch redraws the same round with no operator step (on 07:57 the void lines were stripped by hand). The eras
# below keep the T* behaviour (every trio_up with an n_draw counts), so T*'s sequence never changes under newer code.
VOID_COUNTS_ERAS = frozenset({"t58-restart-2026-10-21"})


def prior_draws(slots_log: Path | None, era: str, record: bool) -> list[dict]:
    """The earlier draws of this era and record flag (trio_up events carrying n_draw), in log order. A record draw
    fails closed (levi 00138): no or an unreadable slots log, or an undecodable trio_up line, raises (k and the
    rotation counters would otherwise restart at 0). Outside VOID_COUNTS_ERAS a run that started no session (no "start"
    event in the log) is not a draw."""
    out, runs, started = [], [], set()
    try:
        if not slots_log:
            raise OSError("no slots log")
        lines = Path(slots_log).read_text().splitlines()
    except OSError as e:
        if record:
            raise RuntimeError(f"n_draw: the slots log {slots_log} is unreadable ({e!r}); a record draw fails closed "
                               f"(an empty file starts an era's first record round)")
        lines = []
    for ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            if record and "trio_up" in ln:
                raise RuntimeError(f"n_draw: an undecodable trio_up line in {slots_log}; a record draw fails closed")
            continue
        if ev.get("event") == "start" and ev.get("run"):
            started.add(ev["run"])
        d = ev.get("n_draw") if ev.get("event") == "trio_up" else None
        if isinstance(d, dict) and d.get("era") == era and bool(d.get("record")) == bool(record):
            out.append(d)
            runs.append(ev.get("run"))
    if era in VOID_COUNTS_ERAS:
        return out
    return [d for d, r in zip(out, runs) if r in started]


def draw_at(k: int, index: dict[int, int], era: str, record: bool, support: tuple[int, ...] = N_SUPPORT,
            seed: int = N_DRAW_SEED, exclude: tuple = ()) -> dict:
    """Round k's draw, given the count of earlier draws of each N in the sequence (index {n: count})."""
    eff = tuple(n for n in support if n not in ROTATING or rotation(n, exclude))
    if not eff:
        raise ValueError(f"support {support} less the excluded compositions {exclude}: nothing left")
    w = weights_of(eff)
    u = random.Random(f"{seed}:{era}:{'record' if record else 'pilot'}:{k}").random()
    acc, n = 0.0, eff[-1]
    for m in eff:
        acc += w[m]
        if u < acc:
            n = m
            break
    i = index.get(n, 0)
    rot = {}
    if n in ROTATING:
        r = rotation(n, exclude)
        rot = {"index": i, "doubled" if n == 4 else "single": r[i % len(r)], "rotation": list(r)}
    return {"rule": N_DRAW_RULE, "seed": seed, "era": era, "record": bool(record), "round": k, "u": round(u, 6),
            "support": list(support), "support_effective": list(eff),
            "weights": {str(m): round(v, 4) for m, v in w.items()}, "n": n,
            "composition": composition(n, i, exclude), **({"rotation": rot} if rot else {}),
            "excluded": [f"{a}:{b}" for a, b in exclude]}


def check_frozen(prior: list[dict], support: tuple[int, ...], exclude: tuple, change_sign: str | None) -> None:
    """levi 00138: a record draw's support and exclusions are the era's last record draw's; a change needs a signed
    reference (--n-change-sign, e.g. levi's ruling on a failed composition) and may only narrow them (an N leaves the
    support, a composition is excluded), never widen."""
    if not prior:
        return
    last = prior[-1]
    ls, lx = set(last.get("support") or ()), set(last.get("excluded") or ())
    s, x = set(support), {f"{a}:{b}" for a, b in exclude}
    if (s, x) == (ls, lx):
        return
    if not change_sign:
        raise ValueError(f"n_draw: support {sorted(s)} / excluded {sorted(x)} differ from the era's last record draw's "
                         f"({sorted(ls)} / {sorted(lx)}): a change needs --n-change-sign")
    if not (s <= ls and x >= lx):
        raise ValueError(f"n_draw: a signed change may only narrow the support / add exclusions (was {sorted(ls)} / "
                         f"{sorted(lx)}, asked {sorted(s)} / {sorted(x)})")


def draw(slots_log: Path | None, era: str, record: bool, support: tuple[int, ...] = N_SUPPORT,
         seed: int = N_DRAW_SEED, exclude: tuple = (), change_sign: str | None = None) -> dict:
    """This round's draw: k and each N's rotation index from the slots log's earlier draws; a record draw's support
    and exclusions frozen per era (check_frozen)."""
    prior = prior_draws(slots_log, era, record)
    if record:
        check_frozen(prior, support, exclude, change_sign)
    idx = {n: sum(1 for d in prior if d.get("n") == n) for n in ROTATING}
    d = draw_at(len(prior), idx, era, record, support, seed, exclude)
    return {**d, "change_sign": change_sign} if change_sign else d


def simulate(rounds: int, era: str, record: bool = True, support: tuple[int, ...] = N_SUPPORT,
             seed: int = N_DRAW_SEED, exclude: tuple = ()) -> list[dict]:
    """A simulated sequence of rounds, each appended to the log as run_trio does (the draws and the rotation)."""
    out, idx = [], {n: 0 for n in ROTATING}
    for k in range(rounds):
        d = draw_at(k, idx, era, record, support, seed, exclude)
        if d["n"] in idx:
            idx[d["n"]] += 1
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
    rot = {n: dict(Counter(list(d["rotation"].values())[1] for d in seq if d["n"] == n)) for n in ROTATING}
    print(f"{a.simulate} rounds, era {a.era}, support {list(parse_support(a.support))}: "
          f"N {dict(sorted(ns.items()))}; N = 4 doubled {rot[4]}; N = 5 single {rot[5]}")
    print(" ".join(f"{d['n']}{list(d['rotation'].values())[1][:2] if 'rotation' in d else ''}" for d in seq))
    return 0


if __name__ == "__main__":
    sys.exit(main())
