#!/usr/bin/env python3
"""W4 (3), levi a9d73b69: downstream packets missing at the capture against the slot's ingress class drops, per session,
from the session dir only (read-only; runs on copies). A downstream packet lost by the ingress netem (`loss P`) or dropped
by the HTB queue never reaches the capture, which taps the IFB after its qdisc; the class's drop counter (tc_stats.jsonl
`in.drops`) counts both. Missing downstream packets:
  - QUIC: packet-number gaps per keyed connection (quic_pn_check's decryption with the session's ssl_keys.log), minus the
    servers' own packet-number skips: a per-packet skip rate measured with --baseline on dry-root sessions whose class
    dropped nothing (Δdrops = 0), applied as rate x the session's downstream QUIC packets;
  - TCP: downstream segments first captured as a retransmission (a (stream, seq, len) never captured before, below the
    stream's highest captured sequence).
  C6, precondition kernel capture drops 0 (meta concurrency.capture_drops):
    - one-sided everywhere: missing <= Δdrops + tol, tol = max(TOL_MIN, TOL_SHARE x Δdrops);
    - two-sided (|missing - Δdrops| <= tol) only when the class carried keyed QUIC alone (no downstream TCP, every QUIC
      connection keyed): repeated drops of the same data and dropped pure ACKs break the >= side elsewhere.
  C5 (netem loss vs P) needs per-sub-step sent counts and is computed from the same rows in a later pass; not here.

    python3 ops/diag/loss_drops.py <session dir> ... [--baseline-json skip.json] [--out report.json]
    python3 ops/diag/loss_drops.py --baseline <dry-root session dir with Δdrops = 0> ... --out skip.json
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

TOL_MIN, TOL_SHARE = 5, 0.02
RULE = ("levi a9d73b69 (W4 3): missing downstream packets (QUIC PN gaps minus the dry-root skip baseline; TCP segments "
        "first seen as retransmissions) vs Δ class drops; C6 one-sided missing <= drops + max(5, 2 %), two-sided only "
        "for keyed-QUIC-only sessions; kernel capture drops must be 0")
HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("quic_pn_check", HERE / "quic_pn_check.py")
QP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(QP)
TCP_FIELDS = ["tcp.stream", "ip.src", "tcp.seq_raw", "tcp.len"]


def _lines(p: Path) -> list[dict]:
    out = []
    for line in (p.read_text().splitlines() if p.exists() else []):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def class_drops(sd: Path) -> int | None:
    d = [r["in"]["drops"] for r in _lines(sd / "tc_stats.jsonl") if isinstance((r.get("in") or {}).get("drops"), (int, float))]
    return int(d[-1] - d[0]) if len(d) >= 2 else None


def tcp_missing(rows: list[list[str]], me: str) -> tuple[int, int]:
    """(downstream TCP data segments, of them first captured as a retransmission of data never captured)."""
    seen: dict[str, set] = collections.defaultdict(set)
    high: dict[str, int] = {}
    seg = miss = 0
    for r in rows:
        r = (r + [""] * 4)[:4]
        st, src, seq, ln = r
        if src == me or not seq.isdigit() or not ln.isdigit() or int(ln) == 0:
            continue
        seg += 1
        s, n = int(seq), int(ln)
        if (s, n) not in seen[st] and st in high and s + n <= high[st]:
            miss += 1
        seen[st].add((s, n))
        high[st] = max(high.get(st, 0), s + n)
    return seg, miss


def quic_down(sd: Path, tshark: str) -> dict:
    """Keyed downstream QUIC: packets and packet-number gaps; connections left undecrypted."""
    rs = QP.rows(sd / "merged.pcap", sd / "ssl_keys.log", tshark)
    cnt = collections.Counter(r[2] for r in rs if len(r) > 2)
    me = cnt.most_common(1)[0][0] if cnt else None
    conns: dict = {}
    for r in rs:
        r = r + [""] * (len(QP.FIELDS) - len(r))
        d = conns.setdefault(r[1], {"down": [], "undecrypted": 0})
        if r[6]:
            d["undecrypted"] += 1
        if r[2] != me:
            d["down"] += [int(x) for x in r[4].split(",") if x.isdigit()]
    keyed = [c for c in conns.values() if not c["undecrypted"]]
    st = [QP.pn_stats(c["down"]) for c in keyed]
    return {"client": me, "connections": len(conns), "keyed": len(keyed),
            "unkeyed": len(conns) - len(keyed), "packets": sum(s.get("packets", 0) for s in st),
            "gaps": sum(s.get("gaps", 0) for s in st)}


def tcp_rows(sd: Path, tshark: str) -> list[list[str]]:
    cmd = [tshark, "-n", "-r", str(sd / "merged.pcap"), "-Y", "tcp.len > 0", "-T", "fields", "-E", "separator=\t",
           "-E", "occurrence=f"] + [x for f in TCP_FIELDS for x in ("-e", f)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def verdict(missing: int, drops: int | None, two_sided: bool, kernel_drops) -> dict:
    if drops is None:
        return {"c6": "not_applicable", "why": "no tc_stats drops"}
    if kernel_drops != 0:                         # unknown or nonzero: the capture itself may have lost packets
        return {"c6": "not_applicable", "why": f"kernel capture drops {kernel_drops}"}
    tol = max(TOL_MIN, TOL_SHARE * drops)
    if missing > drops + tol:
        return {"c6": "fail", "side": "missing > drops + tol", "tol": tol}
    if two_sided and missing < drops - tol:
        return {"c6": "fail", "side": "missing < drops - tol (keyed QUIC only)", "tol": tol}
    return {"c6": "pass", "two_sided": two_sided, "tol": tol}


def session(sd: Path, tshark: str, skip_rate: float = 0.0) -> dict:
    meta = {}
    try:
        meta = json.loads((sd / "meta.json").read_text())
    except (OSError, ValueError):
        pass
    if not (sd / "merged.pcap").exists():
        return {"session": sd.name, "skipped": "missing merged.pcap"}
    q = quic_down(sd, tshark)
    seg, tmiss = tcp_missing(tcp_rows(sd, tshark), q["client"]) if q["client"] else (0, 0)
    qmiss = max(0.0, q["gaps"] - skip_rate * q["packets"])
    drops = class_drops(sd)
    kd = ((meta.get("concurrency") or {}).get("capture_drops") or {}).get("dropped_kernel_total")
    two = seg == 0 and q["unkeyed"] == 0 and q["keyed"] > 0
    missing = round(qmiss + tmiss, 1)
    return {"session": sd.name, "platform": meta.get("platform"), "drops": drops, "kernel_capture_drops": kd,
            "quic": q, "quic_missing_after_skip": round(qmiss, 1), "skip_rate": skip_rate,
            "tcp_segments_down": seg, "tcp_missing": tmiss, "missing": missing, "keyed_quic_only": two,
            **verdict(missing, drops, two, kd)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--tshark", default=shutil.which("tshark") or "tshark")
    ap.add_argument("--baseline", action="store_true", help="measure the PN-skip rate on dry-root sessions with Δdrops = 0")
    ap.add_argument("--baseline-json", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    if a.baseline:
        used, pk, gp, refused = [], 0, 0, []
        for sd in a.sessions:
            if class_drops(sd) != 0:
                refused.append({"session": sd.name, "drops": class_drops(sd)})
                continue
            q = quic_down(sd, a.tshark)
            pk, gp = pk + q["packets"], gp + q["gaps"]
            used.append({"session": sd.name, **q})
        rep = {"rule": "PN-skip baseline: keyed downstream QUIC gaps / packets over sessions with Δdrops = 0",
               "skip_rate": round(gp / pk, 6) if pk else None, "packets": pk, "gaps": gp, "sessions": used,
               "refused_nonzero_drops": refused}
        if a.out:
            a.out.write_text(json.dumps(rep, indent=1))
        print(json.dumps({k: rep[k] for k in ("skip_rate", "packets", "gaps")} | {"refused": len(refused)}))
        return 0 if pk else 2
    skip = json.loads(a.baseline_json.read_text())["skip_rate"] if a.baseline_json else 0.0
    ss = []
    for sd in a.sessions:
        try:
            ss.append(session(sd, a.tshark, skip or 0.0))
        except Exception as e:  # noqa: BLE001 -- one session's failure is reported, named
            ss.append({"session": sd.name, "error": repr(e)[:300]})
    tally = collections.Counter(s.get("c6", "error" if "error" in s else "skipped") for s in ss)
    rep = {"rule": RULE, "skip_rate": skip, "baseline": str(a.baseline_json) if a.baseline_json else None,
           "c6": dict(tally), "sessions": ss}
    if a.out:
        a.out.write_text(json.dumps(rep, indent=1))
    print(json.dumps({"c6": dict(tally), "skip_rate": skip, "fails": [s["session"] for s in ss if s.get("c6") == "fail"]}, indent=1))
    return 0 if any("c6" in s for s in ss) else 2


if __name__ == "__main__":
    sys.exit(main())
