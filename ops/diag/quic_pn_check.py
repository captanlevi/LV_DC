#!/usr/bin/env python3
"""QUIC packet numbers per connection and direction, from a session's merged.pcap decrypted with its ssl_keys.log
(t53 pilot-0b, supervisor: yt_100002's media bodies collapsed in the frames extractor while http_v2 decrypted its
responses; would an egress-mirror duplicate or an IFB reorder break reassembly?). Per QUIC connection (udp.stream) and
direction: packets, distinct packet numbers, duplicates (a number captured twice), gaps (numbers missing between the
first and the last: the shaper's netem loss and real loss show here, in both modes), reorders (a number below the
previous one in capture order), packets left undecrypted; the media connections (googlevideo / ttvnw / tiktok SNI)
summed apart. Read-only; packet numbers and counts only, never payloads.

    python3 ops/diag/quic_pn_check.py <session dir> [<session dir> ...] [--out report.json]
"""
from __future__ import annotations

import argparse
import collections
import ipaddress
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

MEDIA = re.compile(r"(googlevideo\.com|ttvnw\.net|live-video\.net|tiktokcdn\.com|ttlivecdn\.com|fcdnstatic-intl\.com)$")
FIELDS = ["frame.time_epoch", "udp.stream", "ip.src", "quic.header_form", "quic.packet_number",
          "tls.handshake.extensions_server_name", "quic.remaining_payload", "ip.dst"]
EVERY_PACKET_SHARE = 0.99      # client_of: the capture's own address appears in (nearly) every packet


def rows(pcap: Path, keylog: Path | None, tshark: str) -> list[list[str]]:
    cmd = [tshark, "-n", "-r", str(pcap), "-Y", "quic", "-T", "fields", "-E", "separator=\t", "-E", "occurrence=a",
           "-E", "aggregator=,"] + (["-o", f"tls.keylog_file:{keylog}"] if keylog and keylog.exists() else []) \
        + [x for f in FIELDS for x in ("-e", f)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def pn_stats(pns: list[int]) -> dict:
    """Duplicates, gaps and reorders of one direction's packet numbers, in capture order."""
    if not pns:
        return {"packets": 0}
    c = collections.Counter(pns)
    u = sorted(c)
    return {"packets": len(pns), "distinct": len(u), "duplicates": sum(v - 1 for v in c.values() if v > 1),
            "gaps": (u[-1] - u[0] + 1) - len(u), "reorders": sum(1 for a, b in zip(pns, pns[1:]) if b < a)}


def client_of(rs: list[list[str]], meta: dict | None = None) -> tuple[str | None, dict]:
    """The capture's own address and how it was found (levi 17:53: the record where it exists, else inferred, logged):
      - meta: concurrency.client_addr (not recorded at T*: the slots share the host address; a v3 item);
      - inferred_every_packet: the private address that is the source or the destination of >= EVERY_PACKET_SHARE of
        the QUIC packets (a per-host capture's own address is in every packet);
      - inferred_private_src: else the most frequent private source;
      - none.
    The most frequent source overall is the busiest server on a video session (downstream outnumbers upstream), which
    inverted every direction on most yt / twitch record sessions at 2580d451 (peter 2026-10-23)."""
    def private(a: str) -> bool:
        try:
            return ipaddress.ip_address(a).is_private
        except ValueError:
            return False
    rec = ((meta or {}).get("concurrency") or {}).get("client_addr")
    if isinstance(rec, str) and rec:
        return rec, {"client_source": "meta"}
    ends = collections.Counter(a for r in rs for a in {r[2] if len(r) > 2 else "", r[7] if len(r) > 7 else ""}
                               if a and private(a))
    if ends and rs:
        top, n = ends.most_common(1)[0]
        if n / len(rs) >= EVERY_PACKET_SHARE:
            return top, {"client_source": "inferred_every_packet", "client_share": round(n / len(rs), 4)}
    cnt = collections.Counter(r[2] for r in rs if len(r) > 2 and private(r[2]))
    if cnt:
        return cnt.most_common(1)[0][0], {"client_source": "inferred_private_src"}
    return None, {"client_source": "none"}


def session(sd: Path, tshark: str) -> dict:
    rs = rows(sd / "merged.pcap", sd / "ssl_keys.log", tshark)
    meta = {}
    try:                                   # brad 18:16: the record's client address, as loss_drops reads it
        meta = json.loads((sd / "meta.json").read_text())
    except (OSError, ValueError):
        pass
    me, how = client_of(rs, meta)
    conns: dict = {}
    for r in rs:
        r = r + [""] * (len(FIELDS) - len(r))
        st = r[1]
        d = conns.setdefault(st, {"sni": "", "up": [], "down": [], "undecrypted": 0, "packets": 0})
        d["packets"] += 1
        if r[5]:
            d["sni"] = r[5].split(",")[0].lower()
        if r[6]:
            d["undecrypted"] += 1
        for pn in [x for x in r[4].split(",") if x.isdigit()]:     # coalesced packets carry several numbers
            (d["up"] if r[2] == me else d["down"]).append(int(pn))
    per = {}
    tot = {"media": collections.Counter(), "other": collections.Counter()}
    for st, d in conns.items():
        kind = "media" if MEDIA.search(d["sni"] or "") else "other"
        up, down = pn_stats(d["up"]), pn_stats(d["down"])
        per[st] = {"sni": d["sni"], "kind": kind, "packets": d["packets"], "undecrypted": d["undecrypted"],
                   "up": up, "down": down}
        for side, s in (("up", up), ("down", down)):
            for k in ("packets", "duplicates", "gaps", "reorders"):
                tot[kind][f"{side}_{k}"] += s.get(k, 0)
        tot[kind]["undecrypted"] += d["undecrypted"]
        tot[kind]["connections"] += 1
    media = sorted((v for v in per.values() if v["kind"] == "media"), key=lambda v: -v["packets"])
    return {"session": str(sd), "client": me, **how, "quic_packets": len(rs), "connections": len(conns),
            "totals": {k: dict(v) for k, v in tot.items()}, "media_connections": media[:40]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--tshark", default=shutil.which("tshark") or "tshark")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    out = []
    for sd in a.sessions:
        try:
            out.append(session(sd, a.tshark))
        except Exception as e:  # noqa: BLE001 -- one session's failure is reported
            out.append({"session": str(sd), "error": repr(e)[:300]})
        r = out[-1]
        print(json.dumps({"session": r["session"], "totals": r.get("totals"), "error": r.get("error")}), flush=True)
    if a.out:
        a.out.write_text(json.dumps({"what": __doc__.split("\n\n")[0], "sessions": out}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
