#!/usr/bin/env python3
"""flow_overlap.py: the trio's ownership QA (t53; supervisor v3 + design §3, L-A3, L-C6), read from the pcaps only.

Per trio run, over its sessions' merged.pcap and the unmarked remainder's (data/_trio/_default/<run>/merged.pcap):
  - 5-tuple uniqueness: a flow (protocol + both endpoints) seen in two sessions' pcaps while both saw it, or in a
    session's pcap and the default capture at the same time, is a marking leak;
  - QUIC connection-ID uniqueness: a long- or short-header CID seen in two pcaps (a migrated QUIC connection keeps its
    CIDs on a new 5-tuple, which the 5-tuple check cannot see);
  - sibling host families: a TLS / QUIC SNI of another platform's family (check_session MEDIA_HOSTS + PLATFORM_PAGE) in
    a session's pcap;
  - L-A3: a platform's media SNI in the default capture during that platform's session (unshaped media);
  - DNS presence (L-C6): per session, its media host names with a DNS answer, in its own pcap (Chrome's resolver) or in
    the default capture (the system resolver), between its start - DNS_LEAD_S and its end.
Output: JSON on stdout (or --out). Exit 0 = no leak, 1 = a leak, 2 = input missing.

    python3 flow_overlap.py --run <trio run id> [--data DATA_DIR] [--out report.json]
    python3 flow_overlap.py --sessions DIR [DIR ...] --default DEFAULT_DIR
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from check_session import MEDIA_HOSTS, PLATFORM_PAGE   # noqa: E402  (one host-family definition)

DNS_LEAD_S = 120.0
FIELDS = ["frame.time_epoch", "ip.src", "ip.dst", "ipv6.src", "ipv6.dst", "ip.proto", "tcp.srcport", "tcp.dstport",
          "udp.srcport", "udp.dstport", "quic.dcid", "quic.scid", "tls.handshake.extensions_server_name",
          "dns.qry.name", "dns.flags.response", "dns.count.answers"]


def tshark_rows(pcap: Path, tshark: str = "tshark") -> list[dict]:
    """One dict per packet (FIELDS; a field with several occurrences is a comma list)."""
    cmd = [tshark, "-n", "-r", str(pcap), "-T", "fields", "-E", "separator=\t", "-E", "occurrence=a",
           "-E", "aggregator=,"] + [x for f in FIELDS for x in ("-e", f)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [dict(zip(FIELDS, line.split("\t"))) for line in out.splitlines() if line]


def _split(v: str | None) -> list[str]:
    return [x for x in (v or "").split(",") if x]


def flow_key(r: dict) -> tuple | None:
    """(protocol, lower endpoint, higher endpoint): the same key for both directions."""
    src, dst = (_split(r.get("ip.src")) or _split(r.get("ipv6.src")) or [None])[0], \
        (_split(r.get("ip.dst")) or _split(r.get("ipv6.dst")) or [None])[0]
    if not src or not dst:
        return None
    for proto, a, b in (("tcp", "tcp.srcport", "tcp.dstport"), ("udp", "udp.srcport", "udp.dstport")):
        sp, dp = _split(r.get(a)), _split(r.get(b))
        if sp and dp:
            return (proto,) + tuple(sorted([(src, int(sp[0])), (dst, int(dp[0]))]))
    return None


def summarise(rows: list[dict]) -> dict:
    """flows {key: [first, last, packets]}, cids {cid: [first, last]}, snis {name: first}, dns answers {name: [t]},
    dns_packets (how many DNS packets the pcap holds)."""
    flows: dict = {}
    cids: dict = {}
    snis: dict = {}
    dns: dict = {}
    dns_packets = 0
    for r in rows:
        try:
            t = float(r.get("frame.time_epoch") or 0)
        except ValueError:
            continue
        k = flow_key(r)
        if k is not None:
            f = flows.setdefault(k, [t, t, 0])
            f[0], f[1], f[2] = min(f[0], t), max(f[1], t), f[2] + 1
        for c in _split(r.get("quic.dcid")) + _split(r.get("quic.scid")):
            if len(c.replace(":", "")) >= 8:                      # zero-length / tiny CIDs say nothing about owners
                s = cids.setdefault(c, [t, t])
                s[0], s[1] = min(s[0], t), max(s[1], t)
        for n in _split(r.get("tls.handshake.extensions_server_name")):
            snis.setdefault(n.lower(), t)
        names = _split(r.get("dns.qry.name"))
        if names:
            dns_packets += 1
            if "1" in _split(r.get("dns.flags.response")) or "True" in _split(r.get("dns.flags.response")):
                if any(x not in ("", "0") for x in _split(r.get("dns.count.answers"))):
                    for n in names:
                        dns.setdefault(n.lower(), []).append(t)
    return {"flows": flows, "cids": cids, "snis": snis, "dns": dns, "dns_packets": dns_packets}


def family(name: str) -> str | None:
    """The platform whose media or page hosts the name belongs to (None: no platform's)."""
    for p in ("youtube", "twitch", "tiktok", "bilibili"):
        if any(re.search(pt, name) for pt in MEDIA_HOSTS.get(p, [])) or (PLATFORM_PAGE.get(p) and PLATFORM_PAGE[p].search(name)):
            return p
    return None


def is_media(name: str, platform: str) -> bool:
    return any(re.search(pt, name) for pt in MEDIA_HOSTS.get(platform, []))


def _spans_meet(a, b) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def report(sessions: dict[str, dict], default: dict | None) -> dict:
    """sessions: {name: {platform, window: [t0, t1], s: summarise(...)}}; default: summarise() of the default
    capture or None."""
    names = sorted(sessions)
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            fa, fb = sessions[a]["s"]["flows"], sessions[b]["s"]["flows"]
            ca, cb = sessions[a]["s"]["cids"], sessions[b]["s"]["cids"]
            shared = [k for k in fa.keys() & fb.keys() if _spans_meet(fa[k], fb[k])]
            shared_cids = sorted(c for c in ca.keys() & cb.keys())
            pairs.append({"a": a, "b": b, "shared_flows": len(shared), "shared_flow_examples": [list(k) for k in shared[:5]],
                          "shared_quic_cids": len(shared_cids), "cid_examples": shared_cids[:5]})
    per = {}
    for n in names:
        x = sessions[n]
        s, plat, (t0, t1) = x["s"], x["platform"], x["window"]
        fam = {}
        for sni in s["snis"]:
            f = family(sni)
            if f and f != plat:
                fam.setdefault(f, []).append(sni)
        media = sorted(h for h in s["snis"] if is_media(h, plat))

        def answered(h, src):
            return any(t0 - DNS_LEAD_S <= t <= t1 for t in (src or {}).get(h, []))
        own = [h for h in media if answered(h, s["dns"])]
        dflt = [h for h in media if h not in own and default is not None and answered(h, default["dns"])]
        rec = {"platform": plat, "window": [t0, t1], "flows": len(s["flows"]), "quic_cids": len(s["cids"]),
               "sibling_family_snis": fam, "media_hosts": len(media),
               "dns": {"answered_own_pcap": len(own), "answered_default_capture": len(dflt),
                       "unanswered": sorted(set(media) - set(own) - set(dflt)), "dns_packets_in_own_pcap": s["dns_packets"]}}
        if default is not None:
            df = default["flows"]
            rec["flows_also_in_default"] = sum(1 for k, v in s["flows"].items() if k in df and _spans_meet(v, df[k]))
            rec["quic_cids_also_in_default"] = len(s["cids"].keys() & default["cids"].keys())
            rec["default_media_snis_in_window"] = sorted(h for h, t in default["snis"].items()
                                                         if is_media(h, plat) and t0 <= t <= t1)
        per[n] = rec
    leak = (any(p["shared_flows"] or p["shared_quic_cids"] for p in pairs)
            or any(r["sibling_family_snis"] or r.get("flows_also_in_default") or r.get("quic_cids_also_in_default")
                   or r.get("default_media_snis_in_window") for r in per.values()))
    return {"pairs": pairs, "sessions": per, "default_capture": default is not None,
            "default_flows": len(default["flows"]) if default else None, "leak": leak}


def _session_dirs_of_run(data: Path, run: str) -> list[Path]:
    out = []
    for m in list(data.glob("*/*/meta.json")) + list(data.glob("_failed/*/*/meta.json")):
        try:
            if (json.loads(m.read_text()).get("concurrency") or {}).get("trio_run") == run:
                out.append(m.parent)
        except (OSError, ValueError):
            continue
    return sorted(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", default=None, help="a trio run id (meta.json concurrency.trio_run)")
    ap.add_argument("--data", type=Path, default=HERE.parent / "data" / "_trio", help="the trio root (data/_trio)")
    ap.add_argument("--sessions", nargs="*", type=Path, default=[])
    ap.add_argument("--default", type=Path, default=None, help="the run's default-capture dir (merged.pcap)")
    ap.add_argument("--tshark", default=shutil.which("tshark") or "tshark")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    dirs = list(a.sessions)
    dflt = a.default
    if a.run:
        dirs = dirs or _session_dirs_of_run(a.data, a.run)
        dflt = dflt or a.data / "_default" / a.run
    if not dirs:
        print("no sessions", file=sys.stderr)
        return 2
    sessions = {}
    for d in dirs:
        meta = json.loads((d / "meta.json").read_text())
        pcap = d / "merged.pcap"
        if not pcap.exists():
            print(f"{d}: no merged.pcap", file=sys.stderr)
            return 2
        sessions[f"{d.parent.name}/{d.name}"] = {"platform": meta.get("platform"),
                                                 "window": [meta.get("started_at") or 0, meta.get("ended_at") or 1e12],
                                                 "s": summarise(tshark_rows(pcap, a.tshark))}
    default = summarise(tshark_rows(dflt / "merged.pcap", a.tshark)) if dflt and (dflt / "merged.pcap").exists() else None
    rep = {"run": a.run, "session_dirs": [str(d) for d in dirs], "default_dir": str(dflt) if dflt else None,
           **report(sessions, default)}
    text = json.dumps(rep, indent=1, default=str)
    if a.out:
        a.out.write_text(text)
    print(text)
    return 1 if rep["leak"] else 0


if __name__ == "__main__":
    sys.exit(main())
