#!/usr/bin/env python3
"""The per-window stratum draw (collector v3 B5; t69 interface 8b48e25a, levi 2026-10-23 18:02 rulings). Each counted window
is one of three strata: steered (2 / 4), unsteered (1 / 4), lever (1 / 4), in permuted blocks of 4, so every block of four
consecutive windows holds exactly 2 steered, 1 unsteered and 1 lever, and the strata stay balanced through time-of-day and
channel-availability drift. The window's index k is n_draw's round index (the count of earlier draws of this era and record
flag in the slots log), so a void window relaunched before its trio_up keeps its k and its stratum. Block b = k // 4 and
position p = k % 4 (k from 0). The block's order is a Fisher-Yates shuffle of (steered, steered, unsteered, lever) whose
indices come from sha256("v3-stratum|<era>|b<b>|<i>"), portable across Python versions. Nothing observed enters the draw.

ONE function, two checks (levi 18:02, peter 18:03):
- rounds_ref_n computes `draw()` before the launch, logs it, and passes `--expect-stratum <s>`;
- orchestrate recomputes it, refuses before trio_up on a mismatch (or a missing --expect-stratum on a --trio-record run), and
  records it in trio_up and every session's meta.concurrency.window_stratum;
- rounds_ref_n then checks the recorded value.

    python3 stratum_draw.py --slots-log <trio_slots.jsonl> --era <era> [--record]   (this window's draw, JSON)
    python3 stratum_draw.py --simulate 40 --era <era>                               (a simulated sequence)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

STRATA = ("steered", "steered", "unsteered", "lever")
BLOCK = len(STRATA)
SEED_PREFIX = "v3-stratum"
RULE = ("collector v3 B5 (levi 18:02): permuted blocks of 4 (2 steered, 1 unsteered, 1 lever); k = n_draw's round index of "
        "this era and record flag; block b = k // 4, position k % 4; the block's order a Fisher-Yates shuffle with "
        "j_i = int(sha256('v3-stratum|<era>|b<b>|<i>'), 16) % (i + 1), i = 3..1")


def code_sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def block_order(era: str, b: int) -> list[str]:
    order = list(STRATA)
    for i in range(BLOCK - 1, 0, -1):
        j = int(hashlib.sha256(f"{SEED_PREFIX}|{era}|b{b}|{i}".encode()).hexdigest(), 16) % (i + 1)
        order[i], order[j] = order[j], order[i]
    return order


def draw_at(k: int, era: str, record: bool) -> dict:
    if k < 0:
        raise ValueError(f"stratum_draw: k {k} < 0")
    b, p = divmod(k, BLOCK)
    order = block_order(era, b)
    return {"rule": RULE, "era": era, "record": bool(record), "k": k, "block": b, "pos": p, "block_order": order,
            "seed": f"{SEED_PREFIX}|{era}|b{b}", "stratum": order[p], "code": code_sha()}


def draw(slots_log: Path | None, era: str, record: bool) -> dict:
    """This window's stratum: k from the slots log exactly as n_draw counts it (a record draw fails closed on an unreadable
    log, through n_draw.prior_draws)."""
    import n_draw
    return draw_at(len(n_draw.prior_draws(slots_log, era, record)), era, record)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slots-log", type=Path, default=None)
    ap.add_argument("--era", required=True)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--simulate", type=int, default=0)
    a = ap.parse_args(argv)
    if a.simulate:
        for k in range(a.simulate):
            d = draw_at(k, a.era, True)
            print(json.dumps({x: d[x] for x in ("k", "block", "pos", "stratum")}))
        return 0
    try:
        print(json.dumps(draw(a.slots_log, a.era, a.record)))
    except (RuntimeError, ValueError) as e:
        print(f"stratum_draw: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
