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
# levi's ruling on the 5ba0b94 dry run (00132 -> peter's proposal, option A): the qualifying second is the rate stage busy,
# read from the HTB leaf class's own token bucket, not its backlog (a whole segment held in netem's delay line fills the
# backlog while HTB, unable to dequeue it, sits idle with its tokens refilling). At TOKENS_HZ (levi, after 730b0a0a's
# per-second form read tt's ~70-85 %-busy seconds at ~0.85 from their partial edges; set before the next run): a
# sampling interval qualifies when tokens <= 0 at both its samples and bytes moved; the ratio is per state over the
# qualifying intervals (bytes / (rate x their seconds)), support their summed seconds; at 1 Hz (weaker, stated): tokens
# <= 0 at both bounding samples and bytes moving. Validated on the bulk phase of the same run: >= VALID_MIN_SHARE of each
# slot-state's time qualifies and the ratio is within VALID_TOL of the bulk's own, else (A) is not reading the shaper.
# The backlog rule stays a reported third column for one run; (B) per-fetch delivery is reported.
TOKENS_HZ = 10
VALID_MIN_SHARE, VALID_TOL = 0.9, 0.02
FETCH_BDP_MULT = 4


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


def _cls(x: dict, dev: str, cls: str = "1:10") -> dict:
    return ((x.get("classes") or {}).get(dev) or {}).get(cls) or {}


def rate_stage_intervals_10hz(fast: list[dict], dev: str, t0: float, t1: float) -> tuple[list, float]:
    """(qualifying (seconds, bytes) intervals, the window's sampled seconds) under the TOKENS_HZ rule: consecutive samples
    inside [t0, t1] with tokens <= 0 at both and bytes moved; fast: [{t, dev: {bytes, tokens}}]."""
    rows = [x for x in fast if dev in x and t0 <= x["t"] <= t1 and x[dev].get("tokens") is not None
            and x[dev].get("bytes") is not None]
    out = [(b["t"] - a["t"], b[dev]["bytes"] - a[dev]["bytes"]) for a, b in zip(rows, rows[1:])
           if a[dev]["tokens"] <= 0 and b[dev]["tokens"] <= 0 and b[dev]["bytes"] > a[dev]["bytes"]]
    return out, (rows[-1]["t"] - rows[0]["t"]) if len(rows) >= 2 else 0.0


def rate_stage_seconds_1hz(samples: list[dict], dev: str, t0: float, t1: float) -> list:
    """The weaker 1 Hz form: tokens <= 0 at both bounding samples and bytes moving."""
    rows = [x for x in samples if t0 <= x["t"] <= t1]
    out = []
    for a, b in zip(rows, rows[1:]):
        ca, cb = _cls(a, dev), _cls(b, dev)
        if (ca.get("tokens") is not None and cb.get("tokens") is not None and ca["tokens"] <= 0 and cb["tokens"] <= 0
                and cb.get("bytes", 0) > ca.get("bytes", 0)):
            out.append((b["t"] - a["t"], cb["bytes"] - ca["bytes"]))
    return out


def rate_stage_gate(fast: list[dict] | None, samples: list[dict], dev: str, t0: float, t1: float, rate_kbit: float) -> dict:
    """Option A's gate over the window: the 10 Hz rule when the window has fast samples, else the 1 Hz form (weaker)."""
    if fast and any(dev in x and t0 <= x["t"] <= t1 for x in fast):
        q, n = rate_stage_intervals_10hz(fast, dev, t0, t1)
        mode = f"{TOKENS_HZ} Hz intervals: tokens <= 0 at both samples, bytes moved"
    else:
        q, n = rate_stage_seconds_1hz(samples, dev, t0, t1), max(0, int(t1 - t0))
        mode = "1 Hz (weaker): tokens <= 0 at both bounding samples, bytes moving"
    secs = sum(d for d, _ in q)
    ratio = (sum(b for _, b in q) * 8 / 1000 / secs / rate_kbit) if secs and rate_kbit else None
    gate = "insufficient" if secs < MIN_QUAL_S else ("pass" if RATIO_LO <= ratio <= RATIO_HI else "fail")
    return {"mode": mode, "window_s": round(n, 1), "qualifying_share": round(secs / n, 3) if n else None,
            "qualifying_s": round(secs, 1), "ratio": round(ratio, 3) if ratio is not None else None, "gate": gate}


def validate_on_bulk(gate: dict, bulk_ratio: float | None) -> dict:
    """Option A on a bulk slot-state: >= VALID_MIN_SHARE of its seconds qualify and its ratio is within VALID_TOL of the
    bulk's own (realised over set across the window)."""
    share, r = gate.get("qualifying_share"), gate.get("ratio")
    ok = (share is not None and share >= VALID_MIN_SHARE and r is not None and bulk_ratio is not None
          and abs(r - bulk_ratio) <= VALID_TOL + 1e-9)     # both rounded to 3 places: 0.984 - 0.964 is 0.020000...02
    return {"qualifying_share": share, "ratio_a": r, "bulk_ratio": bulk_ratio, "ok": bool(ok)}


def fetch_delivery(segs: list[dict], rate_kbit: float, rtt_s: float) -> dict | None:
    """Option B (reported): for ok fetches >= FETCH_BDP_MULT x BDP, (size / rate + RTT) / actual; 1 = the set rate."""
    bdp = rate_kbit * 1000 / 8 * rtt_s
    r = [((s["bytes"] * 8 / 1000 / rate_kbit) + rtt_s) / (s["t1"] - s["t0"]) for s in segs
         if s.get("ok") and s.get("bytes") and s["bytes"] >= FETCH_BDP_MULT * bdp and s["t1"] > s["t0"] and rate_kbit]
    if not r:
        return None
    within = sum(1 for x in r if RATIO_LO <= x <= RATIO_HI) / len(r)
    return {"n": len(r), "rtt_s": round(rtt_s, 3), "min_bytes": round(FETCH_BDP_MULT * bdp), **(q(r) or {}),
            "share_in_band": round(within, 3)}


def _attempted(segs: list[dict]) -> list[dict]:
    return [s for s in segs if "bytes" in s and "summary" not in s and "start" not in s]


def h3_share(segs: list[dict]) -> float | None:
    att = _attempted(segs)
    return (sum(1 for s in att if s.get("ok") and s.get("alpn") == "h3") / len(att)) if att else None


def h3_attempted(segs: list[dict]) -> int:
    """The attempted segments h3_share is over (the short form's h3 support: >= 20 per slot over its h3 states)."""
    return len(_attempted(segs))


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


def read_state(samples, s: str, k: int, entry: dict, segs: list[dict], state_s: float, phase_t0: float,
               fast: list[dict] | None = None, base_rtt_s: float | None = None) -> dict:
    """One slot's state in the player phase: option A gates (rate_stage_gate); the backlog rule (third column, one
    run) and option B (per-fetch delivery) reported."""
    t0, t1 = k * state_s + 3.0, (k + 1) * state_s - 0.5
    qs, thr = qualifying(samples, f"lvi_{s}", "1:10", t0, t1, entry["rate_kbit"], entry["delay_ms"])
    a_gate = rate_stage_gate(fast, samples, f"lvi_{s}", t0, t1, entry["rate_kbit"])
    rtt_s = 2 * entry["delay_ms"] / 1000 + (base_rtt_s or 0.0)
    seg = [x for x in segs if "t0" in x]
    summ = next((x["summary"] for x in segs if "summary" in x), {})
    share = h3_share(segs)
    h3 = None
    if entry["proto"] == "h3":
        h3 = {"share": round(share, 3) if share is not None else None,
              "gate": "pass" if share is not None and share >= H3_MIN_SHARE else "fail (not read)"}
    b = bursts(samples, f"lvi_{s}", "1:10", seg, entry["rate_kbit"], phase_t0)
    return {"state": entry["state"], "proto": entry["proto"], "rate_kbit": entry["rate_kbit"],
            **{k_: a_gate[k_] for k_ in ("qualifying_s", "ratio", "gate")}, "rate_stage": a_gate,
            "backlog_rule": {"delay_line_threshold_bytes": round(thr), **ratio_gate(qs, entry["rate_kbit"])},
            "fetch_delivery": fetch_delivery(seg, entry["rate_kbit"], rtt_s), "h3": h3,
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
