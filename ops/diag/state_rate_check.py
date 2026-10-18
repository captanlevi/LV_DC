#!/usr/bin/env python3
"""Per shaper state: the session's per-second downstream delivery against the set rate, and the downstream frame sizes
behind it (t53 pilot-0b, levi item 2: twitch / tt read a per-second p95 well above single mode's at state 1 in both
pilots; the trio's per-slot HTB is single mode's `make slow` command for command, so is the gap a measurement one?). From
mergedPackets.csv (Direction 1 = downstream, PacketLength = the captured frame, L2) and net_labels.txt, as gate-2 item 5
reads them: per state, the per-second ratio (n, p50, p95, max, mean; seconds straddling a state change dropped) and
the frame lengths (p50, p90, max, the share above 1514 B: offload-coalesced frames, counted whole in the second their
shaper released them). Run it on trio and single-mode sessions of the same platform. Read-only.

    python3 ops/diag/state_rate_check.py <session dir> ... [--states 1,2] [--out report.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np


def labels(sd: Path) -> list[dict]:
    return [json.loads(line) for line in (sd / "net_labels.txt").read_text().splitlines() if line.strip()]


def per_state(sd: Path, states: set[str] | None) -> dict:
    lab = labels(sd)
    if not lab:
        return {}
    ts_l = [x["timestamp"] for x in lab]
    sec: dict[int, int] = {}
    lens: dict[str, list[int]] = {}
    import bisect
    with open(sd / "mergedPackets.csv") as fh:
        for r in csv.DictReader(fh):
            if str(r.get("Direction")) != "1":
                continue
            t, n = float(r["Timestamp"]), int(r["PacketLength"])
            if t < ts_l[0]:
                continue
            sec[int(t)] = sec.get(int(t), 0) + n
            i = bisect.bisect_right(ts_l, t) - 1
            st = str(lab[i]["state"])
            if states is None or st in states:
                lens.setdefault(st, []).append(n)
    ratios: dict[str, list[float]] = {}
    for s, b in sec.items():
        i0, i1 = bisect.bisect_right(ts_l, s) - 1, bisect.bisect_right(ts_l, s + 1 - 1e-9) - 1
        if i0 < 0 or i0 != i1:
            continue                                     # a second straddling a state change
        st = str(lab[i0]["state"])
        if states is not None and st not in states:
            continue
        ratios.setdefault(st, []).append(b * 8 / 1000 / float(lab[i0]["speed"]))
    out = {}
    for st in sorted(set(ratios) | set(lens), key=int):
        v, ln = np.array(ratios.get(st, [])), np.array(lens.get(st, []))
        out[st] = {"seconds": int(len(v)),
                   "ratio": ({"p50": round(float(np.median(v)), 3), "p95": round(float(np.percentile(v, 95)), 3),
                              "max": round(float(v.max()), 3), "mean": round(float(v.mean()), 3)} if len(v) else None),
                   "frames": int(len(ln)),
                   "frame_len": ({"p50": int(np.median(ln)), "p90": int(np.percentile(ln, 90)), "max": int(ln.max()),
                                  "share_gt_1514": round(float((ln > 1514).mean()), 3)} if len(ln) else None)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--states", default=None, help="e.g. 1,2 (default: every state)")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    states = set(a.states.split(",")) if a.states else None
    rows = []
    for sd in a.sessions:
        try:
            meta = json.loads((sd / "meta.json").read_text())
            rows.append({"session": str(sd), "mode": (meta.get("concurrency") or {}).get("mode", "single"),
                         "per_state": per_state(sd, states)})
        except Exception as e:  # noqa: BLE001 -- one session's failure is reported
            rows.append({"session": str(sd), "error": repr(e)[:300]})
        print(json.dumps(rows[-1]), flush=True)
    if a.out:
        a.out.write_text(json.dumps({"sessions": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
