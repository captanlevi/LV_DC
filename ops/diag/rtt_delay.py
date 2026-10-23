#!/usr/bin/env python3
"""W4 (2), levi a9d73b69: handshake RTT against the netem delay, per session, from the session dir only (read-only; runs
on copies). `make slow` sets netem `delay D` on both directions (the ingress IFB class and the egress class); the
capture taps the egress mirror before the egress netem and the ingress IFB after the ingress netem, so both delays lie
inside the RTT the capture sees: RTT_obs = RTT_path + 2·D.

Per connection, the capture's handshake RTT: TCP SYN out -> SYN-ACK in; QUIC the client's first Initial -> the server's
first long-header packet (header fields only; no keys). D = net_labels' delay_ms in force at the client's first packet;
a handshake within CHANGE_GUARD_S of a delay_ms change (net_labels is a tick log) is dropped (recorded).
  C6: RTT_obs >= 2·D - SLACK_MS on every connection (netem's timer slack + capture timestamping); a violation means
      traffic that bypassed the shaper.
  C5: per platform, the within-/24 slope of RTT_obs on 2·D (connections to one server /24 hold RTT_path fixed; groups
      with one distinct D add nothing) in [SLOPE_LO, SLOPE_HI]; intercept reported.

    python3 ops/diag/rtt_delay.py <session dir> ... [--tshark tshark] [--out report.json]
Exit 0 = read (no gate here), 2 = no handshake read in any session.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import ipaddress
import json
import shutil
import subprocess
import sys
from pathlib import Path

SLACK_MS = 5.0
CHANGE_GUARD_S = 1.0
SLOPE_LO, SLOPE_HI = 0.9, 1.1
RULE = ("levi a9d73b69 (W4 2): RTT_obs = handshake RTT at the capture (TCP SYN -> SYN-ACK, QUIC Initial -> first server "
        "long header); C6 RTT_obs >= 2D - 5 ms per connection; C5 within-/24 slope of RTT_obs on 2D in [0.9, 1.1]")
FIELDS = ["frame.time_epoch", "ip.src", "ip.dst", "tcp.stream", "tcp.flags.syn", "tcp.flags.ack", "udp.stream",
          "quic.header_form", "quic.long.packet_type"]


def tshark_rows(pcap: Path, tshark: str) -> list[list[str]]:
    cmd = [tshark, "-n", "-r", str(pcap), "-Y", "(tcp.flags.syn == 1) or (quic.header_form == 1)", "-T", "fields",
           "-E", "separator=\t", "-E", "occurrence=f"] + [x for f in FIELDS for x in ("-e", f)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [(line.split("\t") + [""] * len(FIELDS))[:len(FIELDS)] for line in out.splitlines() if line]


def is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def client_of(rows: list[list[str]]) -> str | None:
    """The capture's own address: the private address that opens connections (sends SYNs / Initials) most often."""
    c = collections.Counter(r[1] for r in rows if is_private(r[1]) and
                            ((r[4] == "1" and r[5] in ("0", "False")) or r[8] in ("0",)))
    return c.most_common(1)[0][0] if c else None


def handshakes(rows: list[list[str]], me: str) -> list[dict]:
    """[{proto, t, rtt_ms, server}] per connection: the first client opener and the first server reply."""
    opens, replies = {}, {}
    for r in rows:
        try:
            t = float(r[0])
        except ValueError:
            continue
        src, dst = r[1], r[2]
        if r[3]:                                        # TCP
            key = ("tcp", r[3])
            syn, ack = r[4] in ("1", "True"), r[5] in ("1", "True")
            if syn and not ack and src == me:
                opens.setdefault(key, (t, dst))
            elif syn and ack and dst == me:
                replies.setdefault(key, t)
        elif r[6] and r[7] in ("1", "True"):           # QUIC long header
            key = ("quic", r[6])
            if src == me and r[8] == "0":
                opens.setdefault(key, (t, dst))
            elif dst == me:
                replies.setdefault(key, t)
    out = []
    for key, (t0, server) in opens.items():
        t1 = replies.get(key)
        if t1 is not None and t1 >= t0:
            out.append({"proto": key[0], "t": t0, "rtt_ms": round((t1 - t0) * 1000, 3), "server": server})
    return sorted(out, key=lambda x: x["t"])


def delays(sd: Path) -> list[tuple[float, float]]:
    out = []
    p = sd / "net_labels.txt"
    for line in (p.read_text().splitlines() if p.exists() else []):
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if isinstance(x.get("timestamp"), (int, float)) and isinstance(x.get("delay_ms"), (int, float)):
            out.append((float(x["timestamp"]), float(x["delay_ms"])))
    return sorted(out)


def judge(hs: list[dict], dl: list[tuple[float, float]]) -> tuple[list[dict], int]:
    """Attach D (ms) and the C6 verdict to each handshake; handshakes near a change or before the first row dropped.

    net_labels is a tick log (a row every ~2 s), so a change is the first row or a row whose delay_ms differs from the
    previous row's; every row as a change put each handshake within CHANGE_GUARD_S of one and dropped nearly all."""
    ts = [t for t, _ in dl]
    ch = [t for k, (t, d) in enumerate(dl) if k == 0 or d != dl[k - 1][1]]
    kept, dropped = [], 0
    for h in hs:
        i = bisect.bisect_right(ts, h["t"]) - 1
        j = bisect.bisect_right(ch, h["t"]) - 1
        near = any(abs(h["t"] - t) < CHANGE_GUARD_S or abs(h["t"] + h["rtt_ms"] / 1000 - t) < CHANGE_GUARD_S for t in ch[max(0, j - 1):j + 2])
        if i < 0 or near:
            dropped += 1
            continue
        d = dl[i][1]
        kept.append({**h, "delay_ms": d, "c6_ok": h["rtt_ms"] >= 2 * d - SLACK_MS})
    return kept, dropped


def within_slope(rows: list[dict]) -> dict:
    """The within-/24 OLS slope of rtt_ms on 2·delay_ms, and the pooled intercept (mean rtt - slope x mean 2D)."""
    groups = collections.defaultdict(list)
    for r in rows:
        groups[".".join(r["server"].split(".")[:3]) if "." in r["server"] else r["server"]].append(r)
    sxy = sxx = 0.0
    used = 0
    for g in groups.values():
        if len({x["delay_ms"] for x in g}) < 2:
            continue
        mx = sum(2 * x["delay_ms"] for x in g) / len(g)
        my = sum(x["rtt_ms"] for x in g) / len(g)
        sxy += sum((2 * x["delay_ms"] - mx) * (x["rtt_ms"] - my) for x in g)
        sxx += sum((2 * x["delay_ms"] - mx) ** 2 for x in g)
        used += len(g)
    if sxx <= 0:
        return {"slope": None, "connections_used": used, "gate": "insufficient"}
    b = sxy / sxx
    mx = sum(2 * x["delay_ms"] for x in rows) / len(rows)
    my = sum(x["rtt_ms"] for x in rows) / len(rows)
    return {"slope": round(b, 4), "intercept_ms": round(my - b * mx, 2), "connections_used": used,
            "groups": len(groups), "in_band": SLOPE_LO <= b <= SLOPE_HI}


def session(sd: Path, tshark: str) -> dict:
    meta = {}
    try:
        meta = json.loads((sd / "meta.json").read_text())
    except (OSError, ValueError):
        pass
    pcap = sd / "merged.pcap"
    if not pcap.exists():
        return {"session": sd.name, "skipped": "missing merged.pcap"}
    rows = tshark_rows(pcap, tshark)
    me = client_of(rows)
    kept, dropped = judge(handshakes(rows, me), delays(sd)) if me else ([], 0)
    bad = [k for k in kept if not k["c6_ok"]]
    return {"session": sd.name, "platform": meta.get("platform"), "client": me, "handshakes": len(kept),
            "dropped_near_change": dropped, "c6_violations": bad[:50], "n_c6_violations": len(bad), "rows": kept}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--tshark", default=shutil.which("tshark") or "tshark")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    ss = []
    for sd in a.sessions:
        try:
            ss.append(session(sd, a.tshark))
        except Exception as e:  # noqa: BLE001 -- one session's failure is reported, named
            ss.append({"session": sd.name, "error": repr(e)[:300]})
    by_pf = collections.defaultdict(list)
    for s in ss:
        by_pf[s.get("platform") or "unknown"] += s.get("rows") or []
    pooled = {pf: {"connections": len(rs), "c6_violations": sum(1 for r in rs if not r["c6_ok"]), **within_slope(rs)}
              for pf, rs in sorted(by_pf.items()) if rs}
    rep = {"rule": RULE, "pooled": pooled, "sessions": ss}
    if a.out:
        a.out.write_text(json.dumps(rep, indent=1))
    print(json.dumps({"pooled": pooled, "errors": [s["session"] for s in ss if "error" in s],
                      "skipped": [s["session"] for s in ss if "skipped" in s]}, indent=1))
    return 0 if any(s.get("handshakes") for s in ss) else 2


if __name__ == "__main__":
    sys.exit(main())
