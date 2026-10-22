#!/usr/bin/env python3
"""W4 (i), supervisor 03:44: the shaper's realised downstream rate against its configured rate, per session, from the
session dir only (read-only; runs on copies). Over backlogged spans (readers/shaper_math.py, the trace regime's fidelity rule: the slot's ingress class
backlog above its netem delay line + 2 MTU throughout, >= 10 s), three byte counts against the applied rate's integral
(net_labels, each change from its applied time):
  - class: the ingress class's delivered bytes (tc_stats.jsonl, trio_slots.TcSampler), the span ratio;
  - capture: the downstream frames the session's capture holds in the span (mergedPackets.csv, Direction 1,
    PacketLength = the captured L2 frame, as gate-2 item 5 / state_rate_check read them);
  - capture / class: the capture's bytes over the class's (the two counters of one stream).
Reported per span and per state (the per-second capture ratio over backlogged seconds: median, p05, p95), with the
share of spans inside the band [0.9, 1.15]. A read, not a gate: no pass / fail. Stated overheads: both counts
are L2 (the captured frame incl. its 14 B Ethernet header; the class counts skb length at the IFB); the netem delay line
shifts the class's bytes by up to rate x delay against the capture's (reported, not corrected).

    python3 ops/diag/shaper_realised.py <session dir> ... [--out report.json]
Exit 0 = every session read, 2 = none had tc_stats + net_labels + mergedPackets.csv.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "networkSim"))
from readers import shaper_math as F  # noqa: E402  (the one definition of the span arithmetic, levi 04:19)

RULE = ("W4 (i): realised vs configured over backlogged spans (readers/shaper_math) (>= 10 s, backlog above delay line + 2 MTU); "
        "class bytes, capture bytes (mergedPackets.csv Direction 1, L2) and capture/class; band [0.9, 1.15] reported only")


def _lines(p: Path) -> list[dict]:
    out = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def downstream(sd: Path) -> tuple[list[float], list[int]] | None:
    """(timestamps, cumulative L2 bytes) of the downstream frames, time-ordered; None without mergedPackets.csv."""
    p = sd / "mergedPackets.csv"
    if not p.exists():
        return None
    rows = []
    with open(p) as fh:
        for r in csv.DictReader(fh):
            if str(r.get("Direction")) != "1":
                continue
            try:
                rows.append((float(r["Timestamp"]), int(r["PacketLength"])))
            except (KeyError, TypeError, ValueError):
                continue
    rows.sort()
    ts, cum, c = [], [], 0
    for t, n in rows:
        c += n
        ts.append(t)
        cum.append(c)
    return ts, cum


def bytes_in(ds: tuple[list[float], list[int]], t0: float, t1: float) -> int:
    """Capture bytes of frames with t0 < t <= t1."""
    ts, cum = ds
    i, j = bisect.bisect_right(ts, t0), bisect.bisect_right(ts, t1)
    return (cum[j - 1] if j else 0) - (cum[i - 1] if i else 0)


def q(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(p * len(s)))], 4)


def session(sd: Path) -> dict:
    labels = _lines(sd / "net_labels.txt")
    tc = _lines(sd / "tc_stats.jsonl")
    ds = downstream(sd)
    rec: dict = {"session": sd.name, "rows": len(labels), "tc_samples": len(tc), "capture": ds is not None}
    if not labels or not tc or ds is None:
        rec["skipped"] = "missing " + ", ".join(n for n, ok in (("net_labels", labels), ("tc_stats", tc), ("mergedPackets.csv", ds)) if not ok)
        return rec
    changes = F.session_changes(labels)
    samples = F.session_samples(tc)
    state_at = sorted((float(x["timestamp"]), str(x.get("state"))) for x in labels if isinstance(x.get("timestamp"), (int, float)))
    state_ts = [a for a, _ in state_at]
    spans = F.backlogged_spans(samples, changes)
    for s in spans:
        cap = bytes_in(ds, s["t0"], s["t1"]) * 8 / 1000
        s["capture_kbit"] = round(cap, 1)
        s["capture_ratio"] = round(cap / s["applied_kbit"], 4) if s["applied_kbit"] else None
        s["capture_over_class"] = round(cap / s["delivered_kbit"], 4) if s["delivered_kbit"] else None
        k = bisect.bisect_right(state_ts, s["t0"]) - 1
        s["state_at_t0"] = state_at[k][1] if k >= 0 else None
    # per second over backlogged samples: the capture's ratio per state
    hot = {(x["t"]) for sp in spans for x in samples if sp["t0"] <= x["t"] <= sp["t1"]}
    per_state: dict[str, list[float]] = {}
    for a, b in zip(samples, samples[1:]):
        if a["t"] not in hot or b["t"] not in hot or b["t"] <= a["t"]:
            continue
        want = F.integral_kbit_s(changes, a["t"], b["t"])
        if want <= 0:
            continue
        k = bisect.bisect_right(state_ts, a["t"]) - 1
        st = state_at[k][1] if k >= 0 else "None"
        per_state.setdefault(st, []).append(bytes_in(ds, a["t"], b["t"]) * 8 / 1000 / want)
    in_band = lambda key: [s for s in spans if s.get(key) is not None and F.RATIO_LO <= s[key] <= F.RATIO_HI]  # noqa: E731
    rec.update(spans=spans, n_spans=len(spans),
               class_in_band=len(in_band("ratio")), capture_in_band=len(in_band("capture_ratio")),
               per_state_capture_ratio={st: {"seconds": len(v), "p05": q(v, 0.05), "median": q(v, 0.5), "p95": q(v, 0.95)}
                                        for st, v in sorted(per_state.items(), key=lambda x: (not x[0].isdigit(), int(x[0]) if x[0].isdigit() else 0))})
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    rows = [session(sd) for sd in a.sessions]
    read = [r for r in rows if "skipped" not in r]
    spans = [s for r in read for s in r["spans"]]
    pooled = {"sessions": len(rows), "read": len(read), "spans": len(spans),
              "class_ratio": {"p05": q([s["ratio"] for s in spans if s["ratio"]], 0.05),
                              "median": q([s["ratio"] for s in spans if s["ratio"]], 0.5),
                              "p95": q([s["ratio"] for s in spans if s["ratio"]], 0.95)},
              "capture_ratio": {"p05": q([s["capture_ratio"] for s in spans if s["capture_ratio"]], 0.05),
                                "median": q([s["capture_ratio"] for s in spans if s["capture_ratio"]], 0.5),
                                "p95": q([s["capture_ratio"] for s in spans if s["capture_ratio"]], 0.95)},
              "capture_over_class_median": q([s["capture_over_class"] for s in spans if s["capture_over_class"]], 0.5),
              "share_spans_capture_in_band": round(sum(1 for s in spans if s["capture_ratio"] is not None and
                                                       F.RATIO_LO <= s["capture_ratio"] <= F.RATIO_HI) / len(spans), 4) if spans else None}
    rep = {"rule": RULE, "span_rule": F.SPAN_RULE, "pooled": pooled, "sessions": rows}
    if a.out:
        a.out.write_text(json.dumps(rep, indent=1))
    print(json.dumps(pooled, indent=1))
    return 0 if read else 2


if __name__ == "__main__":
    sys.exit(main())
