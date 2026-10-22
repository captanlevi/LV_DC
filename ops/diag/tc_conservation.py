#!/usr/bin/env python3
"""W4 (ii), supervisor 03:44: byte conservation between the slots' shaped ingress classes and the interface, over one
trio window, from session dirs only (read-only; runs on copies). Every slot's TcSampler (trio_slots.py) writes, per
sample, its own ingress class's cumulative counters (`in`: bytes, backlog, drops) and the interface's rx_bytes; the
window's sessions together give every slot's class and one interface counter.

Per window of W seconds (default 10) on a common grid: Δrx = the interface's rx_bytes change (every slot's samples
pooled: one counter); Σ Δclass = the sum over the slots whose session covers the window of their class's bytes change
(cumulative counters, linearly interpolated at the window's ends, never across two sessions); residual = Δrx − Σ Δclass.
A window counts only when every slot with a running session (meta started_at .. ended_at) has samples bracketing it,
none more than MAX_GAP_S (3 s) apart inside it (a sampler stall is never bridged by interpolation).
The residual holds what no slot class carries: the unshaped default class (DNS, management, the box's own traffic), the
orphan drop (prio 40), the shaper's own drops and the netem delay line's in-flight bytes; a negative residual beyond
the delay line's content is the defect signal (a class counting bytes the interface never received). Reported only:
the residual's share of Δrx per window (p05 / median / p95), the windows with a negative residual larger than the
slots' summed delay-line bound, coverage. Overheads: rx_bytes and the class's bytes are both L2 frame lengths (the
14 B Ethernet header included), stated, not corrected.

    python3 ops/diag/tc_conservation.py <session dir> ... [--window-s 10] [--out report.json]
Exit 0 = at least one covered window, 2 = none.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from pathlib import Path

RULE = ("W4 (ii): per W-s window, interface rx_bytes change vs the sum of the covering slots' ingress class bytes "
        "(TcSampler); residual = rx - classes; a window counts only when every running slot brackets it; reported only")
MTU = 1500


def _lines(p: Path) -> list[dict]:
    out = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def interp(ts: list[float], vs: list[float], t: float) -> float | None:
    """The cumulative counter at t, linear between the bracketing samples; None outside the series."""
    if not ts or t < ts[0] or t > ts[-1]:
        return None
    i = bisect.bisect_left(ts, t)
    if ts[i] == t:
        return vs[i]
    a, b = i - 1, i
    return vs[a] + (vs[b] - vs[a]) * (t - ts[a]) / (ts[b] - ts[a])


def max_gap(ts: list[float], t0: float, t1: float) -> float:
    """The longest stretch of [t0, t1] without a sample (the bracketing samples included)."""
    i, j = bisect.bisect_right(ts, t0) - 1, bisect.bisect_left(ts, t1)
    pts = ts[max(i, 0):min(j, len(ts) - 1) + 1]
    return max((b - a for a, b in zip(pts, pts[1:])), default=float("inf"))


def load(sd: Path) -> dict | None:
    meta = {}
    try:
        meta = json.loads((sd / "meta.json").read_text())
    except (OSError, ValueError):
        pass
    rows = sorted((r for r in _lines(sd / "tc_stats.jsonl") if isinstance(r.get("ts"), (int, float))), key=lambda r: r["ts"])
    if not rows:
        return None
    cls = [(r["ts"], (r.get("in") or {}).get("bytes")) for r in rows if isinstance((r.get("in") or {}).get("bytes"), (int, float))]
    rx = [(r["ts"], (r.get("iface") or {}).get("rx_bytes")) for r in rows if isinstance((r.get("iface") or {}).get("rx_bytes"), (int, float))]
    tag = ((meta.get("concurrency") or {}).get("tag")) or sd.name
    delays = [x.get("delay_ms") for x in _lines(sd / "net_labels.txt") if isinstance(x.get("delay_ms"), (int, float))]
    return {"session": sd.name, "tag": tag,
            "start": meta.get("started_at") or (cls[0][0] if cls else None),
            "end": meta.get("ended_at") or (cls[-1][0] if cls else None),
            "max_delay_ms": max(delays) if delays else None,
            "cls_t": [t for t, _ in cls], "cls_b": [float(b) for _, b in cls], "rx": rx,
            "max_rate_kbit": max([v for r in rows for v in (r.get("set_rates_kbit") or {}).values() if isinstance(v, (int, float))] or [0])}


def q(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(p * len(s)))], 4)


def conservation(sessions: list[dict], window_s: float = 10.0, delay_ms: float = 400.0, max_gap_s: float = 3.0) -> dict:
    """sessions: load()'s records of one window. The negative-residual flag's bound per running slot: its largest set
    rate x its largest delay (net_labels; delay_ms when the session has none) + 2 MTU."""
    rx = sorted({p for s in sessions for p in s["rx"]})
    if len(rx) < 2:
        return {"windows": 0, "reason": "no interface samples"}
    rx_t, rx_b = [t for t, _ in rx], [float(b) for _, b in rx]
    grid, t = [], rx_t[0]
    while t + window_s <= rx_t[-1]:
        grid.append((t, t + window_s))
        t += window_s
    out, skipped = [], {"uncovered_slot": 0}
    for t0, t1 in grid:
        d_rx = interp(rx_t, rx_b, t1) - interp(rx_t, rx_b, t0)
        running = [s for s in sessions if s["start"] is not None and s["end"] is not None and s["start"] < t1 and s["end"] > t0]
        tot, ok, bound = 0.0, True, 0.0
        for s in running:
            a, b = interp(s["cls_t"], s["cls_b"], t0), interp(s["cls_t"], s["cls_b"], t1)
            if a is None or b is None or b < a or max_gap(s["cls_t"], t0, t1) > max_gap_s:
                ok = False
                break
            tot += b - a
            bound += s["max_rate_kbit"] * 1000 / 8 * (s.get("max_delay_ms") or delay_ms) / 1000 + 2 * MTU
        if not ok:
            skipped["uncovered_slot"] += 1
            continue
        res = d_rx - tot
        out.append({"t0": round(t0, 3), "t1": round(t1, 3), "rx_bytes": int(d_rx), "class_bytes": int(tot),
                    "slots": sorted(s["tag"] for s in running), "residual": int(res),
                    "residual_share": round(res / d_rx, 4) if d_rx > 0 else None,
                    "negative_beyond_delay_line": res < -bound})
    shares = [w["residual_share"] for w in out if w["residual_share"] is not None]
    return {"windows": len(out), "window_s": window_s, "skipped": skipped,
            "residual_share": {"p05": q(shares, 0.05), "median": q(shares, 0.5), "p95": q(shares, 0.95)},
            "negative_beyond_delay_line": [w for w in out if w["negative_beyond_delay_line"]][:50],
            "n_negative_beyond_delay_line": sum(1 for w in out if w["negative_beyond_delay_line"]),
            "rows": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--window-s", type=float, default=10.0)
    ap.add_argument("--delay-ms", type=float, default=400.0, help="per-slot delay-line bound for the negative flag")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    ss = [s for s in (load(sd) for sd in a.sessions) if s is not None]
    rep = {"rule": RULE, "sessions": [s["session"] for s in ss],
           "missing_tc_stats": [sd.name for sd in a.sessions if sd.name not in {s["session"] for s in ss}],
           **conservation(ss, a.window_s, a.delay_ms)}
    if a.out:
        a.out.write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: rep[k] for k in ("windows", "skipped", "residual_share", "n_negative_beyond_delay_line",
                                          "missing_tc_stats") if k in rep}, indent=1))
    return 0 if rep.get("windows") else 2


if __name__ == "__main__":
    sys.exit(main())
