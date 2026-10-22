"""The trace regime's fidelity gates (levi's rulings efb5e4cc (1)), pure over per-second class samples and the applied
changes, so they test without root. Used by ops/diag/trace_session_check.py (G2's sessions: tc_stats.jsonl and
net_labels.txt) and by the dry run's trace segment (G1).

 - Byte-total ratio over backlogged spans: a span is a maximal run of consecutive samples whose class backlog stays above
   the netem delay line's own content (rate x delay + 2 MTU: fast_segment / player_reading's rule; netem's delay line
   alone keeps a class's backlog above 0 whenever traffic flows), at least MIN_SPAN_S long; ratio = the class's
   delivered bytes / the integral of the applied rate over the span (each change counted from its applied time). Gate:
   every span's ratio in [RATIO_LO, RATIO_HI]; no span: insufficient (not a pass).
 - Apply lag: applied - scheduled per change (net_labels timestamp - scheduled_t); gate p99 <= LAG_P99_MAX_S (at N = 5,
   box CPU recorded beside it by the caller). A lag above the bound fails; it is never fixed by coarsening the trace.
 - The per-second ratio is reported descriptively only.
"""
from __future__ import annotations

import math

MTU = 1500
MIN_SPAN_S = 10.0
RATIO_LO, RATIO_HI = 0.9, 1.15
LAG_P99_MAX_S = 0.5
RULE = ("levi efb5e4cc (1): byte-total ratio delivered / integral of the applied rate over backlogged spans >= 10 s "
        "(backlog above the delay line + 2 MTU throughout), each in [0.9, 1.15]; apply lag p99 <= 0.5 s")


def rate_at(changes: list[tuple[float, float, float]], t: float) -> tuple[float, float] | None:
    """(rate kbit, delay ms) in force at t: the last change applied at or before t. changes: [(applied t, kbit, ms)]."""
    cur = None
    for a, k, d in changes:
        if a <= t:
            cur = (k, d)
        else:
            break
    return cur


def integral_kbit_s(changes: list[tuple[float, float, float]], t0: float, t1: float) -> float:
    """The applied rate's integral over [t0, t1] (kbit), piecewise constant from each change's applied time."""
    pts = [t0] + [a for a, _, _ in changes if t0 < a < t1] + [t1]
    tot = 0.0
    for a, b in zip(pts, pts[1:]):
        r = rate_at(changes, a)
        if r is not None:
            tot += r[0] * (b - a)
    return tot


def backlogged_spans(samples: list[dict], changes: list[tuple[float, float, float]], min_span_s: float = MIN_SPAN_S) -> list[dict]:
    """samples: [{t, bytes, backlog}] of one class, time-ordered; changes: [(applied t, kbit, delay ms)], ordered."""
    def hot(x):
        r = rate_at(changes, x["t"])
        if r is None or x.get("backlog") is None or x.get("bytes") is None:
            return False
        return x["backlog"] > r[0] * 1000 / 8 * r[1] / 1000 + 2 * MTU
    spans, cur = [], []
    for x in samples:
        if hot(x) and (not cur or x["bytes"] >= cur[-1]["bytes"]):
            cur.append(x)
            continue
        if len(cur) >= 2:
            spans.append(cur)
        cur = [x] if hot(x) else []
    if len(cur) >= 2:
        spans.append(cur)
    out = []
    for sp in spans:
        t0, t1 = sp[0]["t"], sp[-1]["t"]
        if t1 - t0 < min_span_s:
            continue
        want = integral_kbit_s(changes, t0, t1)
        got = (sp[-1]["bytes"] - sp[0]["bytes"]) * 8 / 1000
        out.append({"t0": round(t0, 3), "t1": round(t1, 3), "s": round(t1 - t0, 3), "delivered_kbit": round(got, 1),
                    "applied_kbit": round(want, 1), "ratio": round(got / want, 4) if want else None})
    return out


def ratio_gate(spans: list[dict]) -> str:
    rs = [s["ratio"] for s in spans if s["ratio"] is not None]
    if not rs:
        return "insufficient"
    return "pass" if all(RATIO_LO <= r <= RATIO_HI for r in rs) else "fail"


def per_second_ratio(samples: list[dict], changes: list[tuple[float, float, float]]) -> dict:
    """Descriptive only: the ratio per sampled second over the backlogged samples (median and the share in band)."""
    rs = []
    for a, b in zip(samples, samples[1:]):
        if b["t"] <= a["t"] or a.get("bytes") is None or b.get("bytes") is None or b["bytes"] < a["bytes"]:
            continue
        want = integral_kbit_s(changes, a["t"], b["t"])
        if want > 0:
            rs.append((b["bytes"] - a["bytes"]) * 8 / 1000 / want)
    rs.sort()
    return {"seconds": len(rs), "median": round(rs[len(rs) // 2], 4) if rs else None,
            "share_in_band": round(sum(1 for r in rs if RATIO_LO <= r <= RATIO_HI) / len(rs), 4) if rs else None,
            "role": "reported only (levi efb5e4cc (1))"}


def p99(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, math.ceil(0.99 * len(s)) - 1)], 4)


def apply_lags(labels: list[dict]) -> list[float]:
    """applied - scheduled for every net_labels row that carries scheduled_t (a trace change)."""
    return [round(float(x["timestamp"]) - float(x["scheduled_t"]), 4) for x in labels
            if isinstance(x.get("scheduled_t"), (int, float)) and isinstance(x.get("timestamp"), (int, float))]


def lag_gate(lags: list[float]) -> dict:
    p = p99(lags)
    return {"changes": len(lags), "p50": sorted(lags)[len(lags) // 2] if lags else None,
            "p99": p, "max": max(lags) if lags else None, "bound_s": LAG_P99_MAX_S,
            "gate": "insufficient" if p is None else ("pass" if p <= LAG_P99_MAX_S else "fail")}


def session_changes(labels: list[dict]) -> list[tuple[float, float, float]]:
    """net_labels rows to [(applied t, kbit, delay ms)], ordered."""
    out = [(float(x["timestamp"]), float(x["speed"]), float(x.get("delay_ms") or 0.0)) for x in labels
           if isinstance(x.get("timestamp"), (int, float)) and isinstance(x.get("speed"), (int, float))]
    return sorted(out)


def session_samples(tc_rows: list[dict]) -> list[dict]:
    """tc_stats.jsonl rows (trio_slots.TcSampler) to [{t, bytes, backlog}] of the slot's ingress class."""
    out = []
    for r in tc_rows:
        c = r.get("in") or {}
        if isinstance(r.get("ts"), (int, float)) and "bytes" in c and "backlog" in c:
            out.append({"t": float(r["ts"]), "bytes": c["bytes"], "backlog": c["backlog"]})
    return sorted(out, key=lambda x: x["t"])
