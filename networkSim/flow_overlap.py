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
    the default capture (the system resolver), between its start - DNS_LEAD_S and its end;
  - QUIC under the mark (rushi, supervisor 2026-10-19): per session the QUIC (UDP 443) share of its bytes and flows,
    and any UDP packet in the default capture, during the session, to or from an address its media flows used (a QUIC
    flow whose conntrack entry expired arrives unmarked: the conntrack-timeout case); the box's conntrack UDP timeouts.
  - dns_answers.json (levi, gate 2 item 14): every media address the session contacted has a name in it.
Output: JSON on stdout (or --out). Exit 0 = no leak and no DNS fail, 1 = a leak or a DNS fail, 2 = input missing.

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
from trio_slots import conntrack_timeouts                # noqa: E402

DNS_LEAD_S = 120.0
FIELDS = ["frame.time_epoch", "frame.len", "ip.src", "ip.dst", "ipv6.src", "ipv6.dst", "ip.proto", "tcp.srcport", "tcp.dstport",
          "udp.srcport", "udp.dstport", "quic.dcid", "quic.scid", "tls.handshake.extensions_server_name",
          "dns.qry.name", "dns.flags.response", "dns.count.answers", "tcp.flags.str", "quic.header_form", "tcp.len",
          "udp.length"]


def tshark_rows(pcap: Path, tshark: str = "tshark") -> list[dict]:
    """One dict per packet (FIELDS; a field with several occurrences is a comma list)."""
    cmd = [tshark, "-n", "-r", str(pcap), "-T", "fields", "-E", "separator=\t", "-E", "occurrence=a",
           "-E", "aggregator=,"] + [x for f in FIELDS for x in ("-e", f)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [dict(zip(FIELDS, line.split("\t"))) for line in out.splitlines() if line]


def _split(v: str | None) -> list[str]:
    return [x for x in (v or "").split(",") if x]


def link_local_multicast(k: tuple) -> bool:
    """A flow to a link-local multicast group (224.0.0.0/24, ff02::/16): e.g. every Chrome's own mDNS on the protocol's
    fixed 5-tuple (levi's gate-2 call on pilot-1, item 11: reported when packet-disjoint)."""
    return any(str(ip).startswith("224.0.0.") or str(ip).lower().startswith("ff02:") for ip, _ in k[1:])


def _payload(r: dict, k: tuple) -> int | None:
    """The transport payload length: tcp.len, or udp.length less its 8-byte header."""
    try:
        if k[0] == "tcp":
            return int((_split(r.get("tcp.len")) or [""])[0])
        u = (_split(r.get("udp.length")) or [""])[0]
        return int(u) - 8 if u else None
    except ValueError:
        return None


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


def _is_quic(k: tuple) -> bool:
    return k[0] == "udp" and 443 in (k[1][1], k[2][1])


def summarise(rows: list[dict], keep_udp: bool = False, keep_pkts=None) -> dict:
    """flows {key: [first, last, packets, bytes]}, cids {cid: [first, last]}, snis {name: first}, flow_sni {key: name},
    dns answers {name: [t]}, dns_packets, bytes; keep_udp: also every UDP packet as (t, key, bytes) (the default
    capture's QUIC check)."""
    flows: dict = {}
    flow_sni: dict = {}
    udp: list = []
    pkts: dict = {}          # keep_pkts: True (every flow) or a set of keys -> [(t, src, bytes, tcp flags, quic form)]
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
        n = int((_split(r.get("frame.len")) or ["0"])[0] or 0)
        if k is not None:
            f = flows.setdefault(k, [t, t, 0, 0])
            f[0], f[1], f[2], f[3] = min(f[0], t), max(f[1], t), f[2] + 1, f[3] + n
            if keep_udp and k[0] == "udp":
                udp.append((t, k, n))
            if keep_pkts is True or (keep_pkts and k in keep_pkts) or link_local_multicast(k):
                src = (_split(r.get("ip.src")) or _split(r.get("ipv6.src")) or [""])[0]
                pkts.setdefault(k, []).append((t, src, n, (_split(r.get("tcp.flags.str")) or [""])[0],
                                               (_split(r.get("quic.header_form")) or [""])[0], _payload(r, k)))
        for c in _split(r.get("quic.dcid")) + _split(r.get("quic.scid")):
            if len(c.replace(":", "")) >= 8:                      # zero-length / tiny CIDs say nothing about owners
                s = cids.setdefault(c, [t, t])
                s[0], s[1] = min(s[0], t), max(s[1], t)
        for name in _split(r.get("tls.handshake.extensions_server_name")):
            snis.setdefault(name.lower(), t)
            if k is not None:
                flow_sni.setdefault(k, name.lower())
        names = _split(r.get("dns.qry.name"))
        if names:
            dns_packets += 1
            if "1" in _split(r.get("dns.flags.response")) or "True" in _split(r.get("dns.flags.response")):
                if any(x not in ("", "0") for x in _split(r.get("dns.count.answers"))):
                    for n in names:
                        dns.setdefault(n.lower(), []).append(t)
    return {"flows": flows, "cids": cids, "snis": snis, "flow_sni": flow_sni, "dns": dns, "dns_packets": dns_packets,
            "udp": udp, "pkts": pkts}


def _client(s: dict) -> str | None:
    from collections import Counter
    cnt = Counter(ip for k in s["flows"] for ip in (k[1][0], k[2][0]))
    return cnt.most_common(1)[0][0] if cnt else None


IDLE_S = 30.0       # conntrack's UDP timeout at pilot-0 (unreplied); the stream timeout was 120 s


def split_detail(s: dict, default: dict, platform: str) -> dict:
    """Supervisor (i), pilot-0: every flow seen both in the session's pcap and in the default capture: which
    direction's packets sit in the default class and when, relative to the flow in the session's pcap. Per flow a class:
      before_first   default packets before the flow's first packet in the session pcap
      after_last     all after its last one (teardown residue: TIME_WAIT ACKs, RSTs, a server's late packets)
      after_idle     inside the flow, each after a session-side gap > IDLE_S (conntrack expiry: the mark lost)
      mid_flow       inside the flow with no such gap (an owner / marking fault)
    with direction (up = from the client), TCP flags, QUIC header forms and bytes."""
    import collections
    me = _client(s)
    media = {k for k, n in s["flow_sni"].items() if is_media(n, platform)}
    rows, classes = [], collections.Counter()
    for k in sorted(set(s["flows"]) & set(default["flows"])):
        mine = sorted(p[0] for p in s["pkts"].get(k, [])) or [s["flows"][k][0], s["flows"][k][1]]
        dp = default["pkts"].get(k, [])
        if not dp:
            continue
        cls = collections.Counter()
        gaps = []
        for t, src, n, fl, qf, *_ in dp:
            if t < mine[0]:
                cls["before_first"] += 1
            elif t > mine[-1]:
                cls["after_last"] += 1
            else:
                import bisect
                i = bisect.bisect_right(mine, t) - 1
                g = t - mine[i] if i >= 0 else 0.0
                gaps.append(g)
                cls["after_idle" if g > IDLE_S else "mid_flow"] += 1
        main = ("mid_flow" if cls["mid_flow"] else "after_idle" if cls["after_idle"] else
                "before_first" if cls["before_first"] else "after_last")
        classes[main] += 1
        rows.append({"flow": list(k), "media": k in media, "class": main, "per_class": dict(cls),
                     "default_packets": len(dp), "default_bytes": sum(p[2] for p in dp),
                     # levi (pilot-0b): only payload-carrying packets in the default class gate; control ones are reported
                     "payload_packets": sum(1 for p in dp if len(p) > 5 and (p[5] or 0) > 0),
                     "up": sum(p[1] == me for p in dp), "down": sum(p[1] != me for p in dp),
                     "tcp_flags": dict(collections.Counter(p[3] for p in dp if p[3])),
                     "quic_forms": dict(collections.Counter(p[4] for p in dp if p[4])),
                     "max_gap_before_s": round(max(gaps), 2) if gaps else None,
                     # levi (round 1, avahi's mDNS): a link-local multicast flow is a host-wide 5-tuple; its default
                     # packets are another sender's when packet-disjoint (no (time, length) also in the session pcap)
                     **({"link_local_multicast": True, "packets_in_both": len(
                         {(round(p[0], 6), p[2]) for p in dp} & {(round(p[0], 6), p[2]) for p in s["pkts"].get(k, [])})}
                        if link_local_multicast(k) else {}),
                     "session_span": [round(mine[0], 3), round(mine[-1], 3)],
                     "default_span": [round(dp[0][0], 3), round(dp[-1][0], 3)]})
    return {"flows": len(rows), "by_class": dict(classes),
            "media_flows_by_class": dict(collections.Counter(r["class"] for r in rows if r["media"])),
            "default_packets_by_direction": {"up": sum(r["up"] for r in rows), "down": sum(r["down"] for r in rows)},
            "rows": rows[:200]}


def quic_share(s: dict) -> dict:
    fl = s["flows"]
    tot_b = sum(v[3] for v in fl.values())
    q = {k: v for k, v in fl.items() if _is_quic(k)}
    return {"bytes_share": round(sum(v[3] for v in q.values()) / tot_b, 4) if tot_b else None,
            "flows_share": round(len(q) / len(fl), 4) if fl else None, "quic_flows": len(q), "flows": len(fl)}


def media_addresses(s: dict, platform: str) -> set:
    """The remote addresses of the session's media flows (named by SNI): both endpoints less the client's (the address
    on most flows)."""
    from collections import Counter
    cnt = Counter(ip for k in s["flows"] for ip in (k[1][0], k[2][0]))
    me = cnt.most_common(1)[0][0] if cnt else None
    return {ip for k, name in s["flow_sni"].items() if is_media(name, platform) for ip in (k[1][0], k[2][0]) if ip != me}


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
            uni = [k for k in shared if not link_local_multicast(k)]
            mc = []
            for k in shared:
                if link_local_multicast(k):        # packet-disjoint? no (time, length) in both pcaps
                    pa = {(round(x[0], 6), x[2]) for x in sessions[a]["s"]["pkts"].get(k, [])}
                    pb = {(round(x[0], 6), x[2]) for x in sessions[b]["s"]["pkts"].get(k, [])}
                    mc.append({"flow": list(k), "packets_a": len(pa), "packets_b": len(pb), "packets_in_both": len(pa & pb)})
            pairs.append({"a": a, "b": b, "shared_flows": len(shared), "shared_flow_examples": [list(k) for k in shared[:5]],
                          "shared_unicast_flows": len(uni), "shared_link_local_multicast": mc,
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
               "quic": quic_share(s),
               "sibling_family_snis": fam, "media_hosts": len(media),
               "dns": {"answered_own_pcap": len(own), "answered_default_capture": len(dflt),
                       "unanswered": sorted(set(media) - set(own) - set(dflt)), "dns_packets_in_own_pcap": s["dns_packets"]}}
        if default is not None:
            df = default["flows"]
            rec["flows_also_in_default"] = sum(1 for k, v in s["flows"].items() if k in df and _spans_meet(v, df[k]))
            rec["quic_cids_also_in_default"] = len(s["cids"].keys() & default["cids"].keys())
            rec["default_media_snis_in_window"] = sorted(h for h, t in default["snis"].items()
                                                         if is_media(h, plat) and t0 <= t <= t1)
            ips = media_addresses(s, plat)
            hit = [(t, k, b) for t, k, b in default.get("udp") or [] if t0 <= t <= t1 and (k[1][0] in ips or k[2][0] in ips)]
            rec["default_udp_from_media_addresses"] = {"packets": len(hit), "bytes": sum(b for _, _, b in hit),
                                                       "quic_packets": sum(1 for _, k, _ in hit if _is_quic(k)),
                                                       "flows": sorted({str(list(k)) for _, k, _ in hit})[:5],
                                                       "media_addresses": len(ips)}
        dfile = x.get("dns_answers_file")
        if dfile is not None:      # levi (gate 2 item 14): a contacted media address with no name in dns_answers.json
            named = set(json.loads(Path(dfile).read_text())) if Path(dfile).exists() else set()
            unnamed = sorted(media_addresses(s, plat) - named)
            rec["dns_answers_json"] = {"present": Path(dfile).exists(), "addresses": len(named),
                                       "media_addresses": len(media_addresses(s, plat)), "media_unnamed": unnamed}
        per[n] = rec
    leak = (any(p["shared_flows"] or p["shared_quic_cids"] for p in pairs)
            or any(r["sibling_family_snis"] or r.get("flows_also_in_default") or r.get("quic_cids_also_in_default")
                   or r.get("default_media_snis_in_window") or (r.get("default_udp_from_media_addresses") or {}).get("packets")
                   for r in per.values()))
    dns_fail = [n for n, r in per.items() if (r.get("dns_answers_json") or {}).get("media_unnamed")
                or (r.get("dns_answers_json") or {}).get("present") is False]
    return {"pairs": pairs, "sessions": per, "default_capture": default is not None,
            "default_flows": len(default["flows"]) if default else None, "leak": leak, "dns_fail": dns_fail}


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
    ap.add_argument("--detail", action="store_true", help="the split flows' packets in the default class (split_detail)")
    a = ap.parse_args(argv)
    dirs = list(a.sessions)
    dflt = a.default
    if a.run:
        dirs = dirs or _session_dirs_of_run(a.data, a.run)
        dflt = dflt or a.data / "_default" / a.run
    if not dirs:
        print("no sessions", file=sys.stderr)
        return 2
    default = None
    if dflt and (dflt / "merged.pcap").exists():
        default = summarise(tshark_rows(dflt / "merged.pcap", a.tshark), keep_udp=True, keep_pkts=True if a.detail else None)
    sessions = {}
    for d in dirs:
        meta = json.loads((d / "meta.json").read_text())
        pcap = d / "merged.pcap"
        if not pcap.exists():
            print(f"{d}: no merged.pcap", file=sys.stderr)
            return 2
        sessions[f"{d.parent.name}/{d.name}"] = {"platform": meta.get("platform"),
                                                 "window": [meta.get("started_at") or 0, meta.get("ended_at") or 1e12],
                                                 "s": summarise(tshark_rows(pcap, a.tshark), keep_pkts=(
                                                     set(default["flows"]) if a.detail and default else None)),
                                                 "dns_answers_file": str(d / "dns_answers.json")}
    rep = {"run": a.run, "session_dirs": [str(d) for d in dirs], "default_dir": str(dflt) if dflt else None,
           "conntrack_timeouts_now": conntrack_timeouts(), **report(sessions, default)}
    if a.detail and default:
        rep["split_detail"] = {n: split_detail(x["s"], default, x["platform"]) for n, x in sessions.items()}
    text = json.dumps(rep, indent=1, default=str)
    if a.out:
        a.out.write_text(text)
    print(text)
    return 1 if rep["leak"] or rep["dns_fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
