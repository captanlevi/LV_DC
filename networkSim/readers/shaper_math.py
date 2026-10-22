"""The shaper's span arithmetic, one definition for every reader (levi 04:19, t64): pure functions over per-second class
samples and the applied changes, so they test without root. Moved here byte for byte from the trace regime's
traces/fidelity.py (peter/trace-regime f9d8f11), which re-imports them when it rebases; the W4 readers
(ops/diag/shaper_realised.py, tc_conservation.py) import them from here.

 - A backlogged span: a maximal run of consecutive samples whose class backlog stays above the netem delay line's own
   content (rate x delay + 2 MTU), at least MIN_SPAN_S long; ratio = the class's delivered bytes / the integral of the
   applied rate over the span (each change counted from its applied time). Band [RATIO_LO, RATIO_HI] (levi efb5e4cc (1)).
"""
from __future__ import annotations

MTU = 1500
MIN_SPAN_S = 10.0
RATIO_LO, RATIO_HI = 0.9, 1.15
SPAN_RULE = ("levi efb5e4cc (1): byte-total ratio delivered / integral of the applied rate over backlogged spans >= 10 s "
             "(backlog above the delay line + 2 MTU throughout), band [0.9, 1.15]")


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

