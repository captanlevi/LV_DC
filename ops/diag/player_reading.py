"""The dry run's player-load reading (Q am. 16, master 62d949a9; peter's spec with levi's four changes, 2026-10-20). Pure
functions over trio_dry_run's per-second samples, the player's per-segment logs and the phase's snaplen-96 captures
(tcpdump -nn -q -r text), so they test without root.

Gated (per slot and state, in both loads):
  - the byte ratio (realised / set) in [RATIO_LO, RATIO_HI] over qualifying seconds only. A qualifying second's class
    backlog exceeds the state's netem delay line (set rate x delay) by >= 2 MTU at BOTH of its bounding samples (drops
    are no bound signal: netem's loss raises them). Fewer than MIN_QUAL_S qualifying seconds: "insufficient" (reported;
    the bulk load's ratio stands as that state's gate);
  - h3 actually exercised: in an h3-assigned state >= H3_MIN_SHARE of the attempted segments went over h3 (negotiated
    ALPN), else the "h3 lands in its own class" check is not read: a FAIL;
  - no leak: no packet of a slot's player 5-tuples (remote ip:port, local port) on another slot's ingress capture or on
    the default class's.
Reported: the post-pause burst (class bytes above the set rate in the BURST_S after each fetch that follows a pause),
mean and max per state; per-fetch completion time over size / rate; QUIC vs TCP ratios; the player's stalls.
"""
from __future__ import annotations

import re

MTU = 1500
RATIO_LO, RATIO_HI = 0.9, 1.15
MIN_QUAL_S = 10
H3_MIN_SHARE = 0.9
BURST_S, PAUSE_S = 2.0, 0.5


def delay_line_bytes(rate_kbit: float, delay_ms: float) -> float:
    return rate_kbit * 1000 / 8 * delay_ms / 1000


def qualifying(samples: list[dict], dev: str, cls: str, t0: float, t1: float, rate_kbit: float, delay_ms: float):
    """[(seconds, bytes)] of the window's qualifying seconds (module docstring), and the threshold used."""
    thr = delay_line_bytes(rate_kbit, delay_ms) + 2 * MTU
    rows = [x for x in samples if t0 <= x["t"] <= t1]
    out = []
    for a, b in zip(rows, rows[1:]):
        ca = ((a.get("classes") or {}).get(dev) or {}).get(cls) or {}
        cb = ((b.get("classes") or {}).get(dev) or {}).get(cls) or {}
        if ca.get("backlog", 0) > thr and cb.get("backlog", 0) > thr and "bytes" in ca and "bytes" in cb:
            out.append((b["t"] - a["t"], cb["bytes"] - ca["bytes"]))
    return out, thr


def ratio_gate(q: list[tuple[float, int]], rate_kbit: float) -> dict:
    secs = sum(d for d, _ in q)
    ratio = (sum(b for _, b in q) * 8 / 1000 / secs / rate_kbit) if secs and rate_kbit else None
    if len(q) < MIN_QUAL_S:
        gate = "insufficient"
    else:
        gate = "pass" if RATIO_LO <= ratio <= RATIO_HI else "fail"
    return {"qualifying_s": len(q), "ratio": round(ratio, 3) if ratio is not None else None, "gate": gate}


def h3_share(segs: list[dict]) -> float | None:
    att = [s for s in segs if "bytes" in s and "summary" not in s and "start" not in s]
    return (sum(1 for s in att if s.get("ok") and s.get("alpn") == "h3") / len(att)) if att else None


def five_tuples(segs: list[dict]) -> set[tuple]:
    """(transport, remote ip, remote port, local port) of every logged segment's connection."""
    out = set()
    for s in segs:
        t = s.get("five_tuple") or {}
        try:
            rip, rport = t["remote"].rsplit(":", 1)
            rip = rip[7:] if rip.lower().startswith("::ffff:") else rip     # an IPv4-mapped address: tcpdump prints IPv4
            lport = t["local"].rsplit(":", 1)[1]
            out.add((t["transport"], rip, int(rport), int(lport)))
        except Exception:  # noqa: BLE001
            continue
    return out


_PKT = re.compile(r"IP6? (\S+)\.(\d+) > (\S+)\.(\d+): (UDP|tcp|TCP|Flags)", re.I)


def parse_tcpdump(text: str) -> list[tuple]:
    """(transport, src ip, src port, dst ip, dst port) per packet line of `tcpdump -nn -q -r` (IPv4 / IPv6)."""
    out = []
    for line in text.splitlines():
        m = _PKT.search(line)
        if m:
            tr = "udp" if m.group(5).upper() == "UDP" else "tcp"
            out.append((tr, m.group(1), int(m.group(2)), m.group(3), int(m.group(4))))
    return out


def leaks(tuples_by_slot: dict[str, set], packets_by_capture: dict[str, list]) -> dict:
    """{slot: {capture: matched packets}} on every ingress capture but the slot's own (lvi_<slot>): a downstream
    packet matches a player tuple when its source is the tuple's remote ip:port and its destination port the local port."""
    out = {}
    for slot, tups in tuples_by_slot.items():
        keys = {(tr, rip, rport, lport) for tr, rip, rport, lport in tups}
        row = {}
        for cap, pkts in packets_by_capture.items():
            if cap == f"lvi_{slot}":
                continue
            row[cap] = sum(1 for tr, sip, sport, _, dport in pkts if (tr, sip, sport, dport) in keys)
        out[slot] = row
    return out


def _bytes_between(samples, dev, cls, a, b):
    rows = [x for x in samples if a - 1.0 <= x["t"] <= b + 1.0]
    pts = [(x["t"], ((x.get("classes") or {}).get(dev) or {}).get(cls, {}).get("bytes")) for x in rows]
    pts = [p for p in pts if p[1] is not None]
    if len(pts) < 2:
        return None

    def at(t):
        lo = max((p for p in pts if p[0] <= t), default=pts[0], key=lambda p: p[0])
        hi = min((p for p in pts if p[0] >= t), default=pts[-1], key=lambda p: p[0])
        if hi[0] == lo[0]:
            return lo[1]
        return lo[1] + (hi[1] - lo[1]) * (t - lo[0]) / (hi[0] - lo[0])
    return at(b) - at(a)


def bursts(samples, dev, cls, segs, rate_kbit, phase_t0: float) -> list[float]:
    """Bytes above the set rate in the BURST_S after each fetch that starts >= PAUSE_S after the previous one ended."""
    out, prev_end = [], None
    for s in segs:
        if "t0" not in s:
            continue
        t0 = s["t0"] - phase_t0
        if prev_end is not None and t0 - prev_end >= PAUSE_S:
            b = _bytes_between(samples, dev, cls, t0, t0 + BURST_S)
            if b is not None:
                out.append(max(0.0, b - rate_kbit * 1000 / 8 * BURST_S))
        prev_end = s["t1"] - phase_t0
    return out


def fetch_ratios(segs, rate_kbit) -> list[float]:
    """Per ok segment: its fetch time over size / set rate (1 = at the set rate)."""
    return [(s["t1"] - s["t0"]) / (s["bytes"] * 8 / 1000 / rate_kbit) for s in segs
            if s.get("ok") and s.get("bytes") and rate_kbit]


def q(xs, ps=(50, 90)):
    xs = sorted(xs)
    return {f"p{p}": round(xs[min(len(xs) - 1, int(p / 100 * len(xs)))], 3) for p in ps} if xs else None


def read_state(samples, s: str, k: int, entry: dict, segs: list[dict], state_s: float, phase_t0: float) -> dict:
    """One slot's state in the player phase: the gated readings and the reported ones."""
    t0, t1 = k * state_s + 3.0, (k + 1) * state_s - 0.5
    qs, thr = qualifying(samples, f"lvi_{s}", "1:10", t0, t1, entry["rate_kbit"], entry["delay_ms"])
    seg = [x for x in segs if "t0" in x]
    summ = next((x["summary"] for x in segs if "summary" in x), {})
    share = h3_share(segs)
    h3 = None
    if entry["proto"] == "h3":
        h3 = {"share": round(share, 3) if share is not None else None,
              "gate": "pass" if share is not None and share >= H3_MIN_SHARE else "fail (not read)"}
    b = bursts(samples, f"lvi_{s}", "1:10", seg, entry["rate_kbit"], phase_t0)
    return {"state": entry["state"], "proto": entry["proto"], "rate_kbit": entry["rate_kbit"],
            "delay_line_threshold_bytes": round(thr), **ratio_gate(qs, entry["rate_kbit"]), "h3": h3,
            "segments": summ.get("segments"), "errors": summ.get("errors"), "stalls": summ.get("stalls"),
            "stall_s": summ.get("stall_s"), "behind_max": max((x.get("behind", 0) for x in seg), default=None),
            "fallback_segments": sum(1 for x in seg if x.get("fallback")),
            "post_pause_burst_bytes": {"n": len(b), "mean": round(sum(b) / len(b)) if b else None,
                                       "max": round(max(b)) if b else None},
            "fetch_time_over_size_rate": q(fetch_ratios(seg, entry["rate_kbit"]))}


NOTES = ("the QUIC stack is aioquic's, not Chrome's (congestion control, pacing): a QUIC reading is of the shaper and the "
         "class; twitch's as-produced half (chunked, progressive at the rung rate) is not emulated: the whole-segment "
         "model is the shaper-stressing one; a qualifying second needs the class backlog above the netem delay line by "
         ">= 2 MTU at both bounding samples (drops no signal)")
