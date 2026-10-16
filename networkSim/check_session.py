"""check_session.py: post-session acceptance check for collection v2 (LVQoE t35; brief collection_v2 §2).

    python check_session.py <session_dir> [--move-failed] [--max-background-share X]

Writes <session_dir>/check.json and a one-screen check.md. Verdict PASS / FAIL with reasons. With --move-failed a FAIL
moves the session dir to data/_failed/<platform>/ (archive, never delete).

Checks (thresholds in CHECKS below; the background-traffic threshold comes from the t36 prereg and is not enforced
until it is passed in):
  pcap      merged.pcap present, bytes per minute
  keys      ssl_keys.log present (kept in v2); http_v2.json request count; share of downstream TLS/QUIC bytes on
            flows the decoded log covers
  labels    res_meta at ~1 Hz; current_time advancing between ticks while playing; ad share; stall share; tier mix;
            tier changes per minute (ad ticks excluded from tiers)
  shaper    per net_labels episode: measured downstream kbit/s vs the set rate; binds if measured <= 1.1 x set in
            >= 0.9 of episodes
  shots     screenshots present vs one per 10 s
  background downstream bytes outside the flows that carry media requests in http_v2.json
"""
from __future__ import annotations

import argparse
import collections
import csv
import ipaddress
import json
import shutil
import subprocess
import sys
from pathlib import Path

CHECKS = {
    "res_meta_hz_min": 0.8,
    "advancing_share_min": 0.8,
    "shaper_ratio_max": 1.1,
    "shaper_episode_share_min": 0.9,
    "shots_share_min": 0.5,
    "shot_interval_s": 10.0,
}
MEDIA_PATTERNS = ("/videoplayback", ".ts", ".m4s", ".flv", "/v1/segment/")
TSHARK = shutil.which("tshark") or "tshark"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def read_labels(sd: Path) -> list[dict]:
    p = sd / "labels.csv"
    if not p.exists():
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def label_checks(rows: list[dict]) -> dict:
    meta = [r for r in rows if r.get("type") == "res_meta"]
    if len(meta) < 2:
        return {"n_res_meta": len(meta)}
    ts = [(_f(r["ts"]) or 0) / 1000.0 for r in meta]
    dur = max(ts) - min(ts)
    has_v2 = "is_ad" in meta[0]
    is_ad = [str(r.get("is_ad", "")).lower() == "true" for r in meta]
    # stall intervals from the event stream: stall -> next start
    stalled_at = []
    state = False
    for r in rows:
        if r.get("type") == "stall":
            state = True
        elif r.get("type") in ("start", "video_start"):
            state = False
        elif r.get("type") == "res_meta":
            stalled_at.append(state)
    ct = [_f(r.get("current_time")) for r in meta]
    adv = [b is not None and a is not None and b > a for a, b in zip(ct, ct[1:])]
    playing = [not s for s in stalled_at[1:]]
    adv_play = [a for a, p in zip(adv, playing) if p]
    tiers = [int(_f(r.get("video_h")) or 0) for r, ad in zip(meta, is_ad) if not ad]
    changes = sum(1 for a, b in zip(tiers, tiers[1:]) if a and b and a != b)
    mix = collections.Counter(t for t in tiers if t)
    n = max(1, sum(mix.values()))
    return {
        "n_res_meta": len(meta), "duration_s": dur, "res_meta_hz": (len(meta) - 1) / dur if dur > 0 else None,
        "advancing_share_while_playing": (sum(adv_play) / len(adv_play)) if adv_play else None,
        "v2_columns": has_v2, "ad_share": (sum(is_ad) / len(is_ad)) if has_v2 else None,
        "ad_signals": dict(collections.Counter(s for r in meta for s in (r.get("ad_signals") or "").split("|") if s)),
        "stall_share": sum(stalled_at) / len(stalled_at) if stalled_at else None,
        "tier_mix": {str(k): v / n for k, v in sorted(mix.items())},
        "tier_changes_per_min": changes / (dur / 60.0) if dur > 0 else None,
        "src_changed_ticks": sum(1 for r in meta if str(r.get("src_changed", "")).lower() == "true"),
    }


def packets(pcap: Path):
    """(time, src, dst, sport, dport, proto, length) per packet via tshark fields."""
    cmd = [TSHARK, "-r", str(pcap), "-T", "fields", "-E", "separator=,",
           "-e", "frame.time_epoch", "-e", "ip.src", "-e", "ip.dst", "-e", "ipv6.src", "-e", "ipv6.dst",
           "-e", "tcp.srcport", "-e", "tcp.dstport", "-e", "udp.srcport", "-e", "udp.dstport", "-e", "frame.len"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in proc.stdout:
        f = line.rstrip("\n").split(",")
        if len(f) < 10:
            continue
        t = _f(f[0]); ln = _f(f[9])
        src, dst = (f[1] or f[3]), (f[2] or f[4])
        if f[5]:
            sp, dp, pr = f[5], f[6], "TCP"
        elif f[7]:
            sp, dp, pr = f[7], f[8], "UDP"
        else:
            continue
        if t is None or ln is None or not src or not dst:
            continue
        yield t, src, dst, sp, dp, pr, ln
    proc.wait()


def client_ip(pkts) -> str | None:
    c = collections.Counter()
    for _, s, d, *_ in pkts:
        for ip in (s, d):
            try:
                if ipaddress.ip_address(ip).is_private:
                    c[ip] += 1
            except ValueError:
                pass
    return c.most_common(1)[0][0] if c else None


def media_flow_keys(sd: Path) -> tuple[set, int]:
    """Client-side 5-tuples (src, sport, dst, dport, proto) that carry media requests in http_v2.json."""
    p = sd / "http_v2.json"
    if not p.exists() or p.stat().st_size < 10:
        return set(), 0
    keys, n = set(), 0
    for key, recs in json.load(open(p)).items():
        for r in recs if isinstance(recs, list) else []:
            n += 1
            url = (r.get("request_url") or "").split("?")[0]
            if any(m in url for m in MEDIA_PATTERNS):
                try:
                    a, b = key.split("->")
                    sip, sport = a.split("$")
                    dip, dport, proto = b.split("$")
                    keys.add((sip, sport, dip, dport, proto))
                except ValueError:
                    pass
    return keys, n


def traffic_checks(sd: Path) -> dict:
    pcap = sd / "merged.pcap"
    if not pcap.exists():
        return {"pcap": False}
    pk = list(packets(pcap))
    if not pk:
        return {"pcap": True, "packets": 0}
    me = client_ip(pk)
    t0, t1 = min(p[0] for p in pk), max(p[0] for p in pk)
    media, n_req = media_flow_keys(sd)
    down = [p for p in pk if p[2] == me]
    down_total = sum(p[6] for p in down)
    down_media = sum(p[6] for p in down if (p[2], p[4], p[1], p[3], p[5]) in media)
    tls_down = sum(p[6] for p in down if p[3] == "443")
    logged_flows = set()
    if (sd / "http_v2.json").exists() and (sd / "http_v2.json").stat().st_size > 10:
        for key in json.load(open(sd / "http_v2.json")):
            try:
                a, b = key.split("->")
                sip, sport = a.split("$"); dip, dport, proto = b.split("$")
                logged_flows.add((sip, sport, dip, dport, proto))
            except ValueError:
                pass
    tls_logged = sum(p[6] for p in down if p[3] == "443" and (p[2], p[4], p[1], p[3], p[5]) in logged_flows)
    # shaper binding per net_labels episode
    eps = []
    nl = sd / "net_labels.txt"
    if nl.exists():
        lab = [json.loads(l) for l in nl.read_text().splitlines() if l.strip()]
        for a, b in zip(lab, lab[1:] + [{"timestamp": t1}]):
            lo, hi = a["timestamp"], b["timestamp"]
            if hi - lo < 10:
                continue
            byts = sum(p[6] for p in down if lo <= p[0] < hi)
            kbit = byts * 8 / 1000 / (hi - lo)
            eps.append({"state": a.get("state"), "set_kbit": a.get("speed"), "measured_kbit": kbit,
                        "ratio": kbit / a["speed"] if a.get("speed") else None})
    binds = [e["ratio"] is not None and e["ratio"] <= CHECKS["shaper_ratio_max"] for e in eps]
    return {
        "pcap": True, "pcap_bytes": pcap.stat().st_size, "minutes": (t1 - t0) / 60.0,
        "pcap_bytes_per_min": pcap.stat().st_size / max((t1 - t0) / 60.0, 1e-9), "client_ip": me,
        "ssl_keys": (sd / "ssl_keys.log").exists(), "http_v2_records": n_req,
        "tls_down_share_logged": tls_logged / tls_down if tls_down else None,
        "background_share": 1 - down_media / down_total if down_total else None,
        "episodes": eps, "shaper_bind_share": (sum(binds) / len(binds)) if binds else None,
    }


def verdict(lab: dict, net: dict, shots: dict, max_bg: float | None) -> tuple[str, list[str]]:
    why = []
    if not net.get("pcap") or not net.get("packets", 1):
        why.append("no pcap / no packets")
    if not net.get("ssl_keys"):
        why.append("ssl_keys.log missing (v2 keeps the keys)")
    if not lab.get("n_res_meta"):
        why.append("no res_meta ticks")
    else:
        if (lab.get("res_meta_hz") or 0) < CHECKS["res_meta_hz_min"]:
            why.append(f"res_meta rate {lab.get('res_meta_hz')} < {CHECKS['res_meta_hz_min']} Hz")
        a = lab.get("advancing_share_while_playing")
        if a is not None and a < CHECKS["advancing_share_min"]:
            why.append(f"current_time advancing in {a:.2f} of playing ticks < {CHECKS['advancing_share_min']}")
        if not lab.get("v2_columns"):
            why.append("labels.csv lacks the v2 columns (old extension loaded?)")
    sb = net.get("shaper_bind_share")
    if sb is not None and sb < CHECKS["shaper_episode_share_min"]:
        why.append(f"shaper binds in {sb:.2f} of episodes < {CHECKS['shaper_episode_share_min']}")
    if shots.get("share") is not None and shots["share"] < CHECKS["shots_share_min"]:
        why.append(f"screenshots {shots['n']} of ~{shots['expected']} expected")
    bg = net.get("background_share")
    if max_bg is not None and bg is not None and bg > max_bg:
        why.append(f"background traffic share {bg:.3f} > {max_bg}")
    return ("FAIL" if why else "PASS"), why


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--move-failed", action="store_true")
    ap.add_argument("--max-background-share", type=float, default=None,
                    help="set by the t36 prereg; not enforced when absent (reported only)")
    a = ap.parse_args()
    sd = a.session_dir.resolve()
    lab = label_checks(read_labels(sd))
    net = traffic_checks(sd)
    shot_files = list((sd / "shots").glob("*.jpg")) if (sd / "shots").exists() else []
    dur = lab.get("duration_s") or (net.get("minutes") or 0) * 60
    exp = int(dur / CHECKS["shot_interval_s"]) if dur else 0
    shots = {"n": len(shot_files), "expected": exp, "share": (len(shot_files) / exp) if exp else None}
    v, why = verdict(lab, net, shots, a.max_background_share)
    out = {"session": str(sd), "verdict": v, "reasons": why, "thresholds": CHECKS,
           "max_background_share": a.max_background_share, "labels": lab, "traffic": net, "shots": shots}
    (sd / "check.json").write_text(json.dumps(out, indent=1, default=str))
    md = [f"# check: {sd.name} — **{v}**", ""] + [f"- {r}" for r in why] + ["",
          "| item | value |", "|---|---|",
          f"| duration (min) | {dur / 60:.1f} |" if dur else "| duration | — |",
          f"| res_meta Hz | {lab.get('res_meta_hz')} |",
          f"| current_time advancing (playing) | {lab.get('advancing_share_while_playing')} |",
          f"| ad share / signals | {lab.get('ad_share')} / {lab.get('ad_signals')} |",
          f"| stall share | {lab.get('stall_share')} |",
          f"| tier mix | {lab.get('tier_mix')} |",
          f"| tier changes / min | {lab.get('tier_changes_per_min')} |",
          f"| pcap bytes / min | {net.get('pcap_bytes_per_min')} |",
          f"| ssl keys kept | {net.get('ssl_keys')} |",
          f"| http_v2 records / TLS down share logged | {net.get('http_v2_records')} / {net.get('tls_down_share_logged')} |",
          f"| background traffic share | {net.get('background_share')} |",
          f"| shaper binds (share of episodes) | {net.get('shaper_bind_share')} |",
          f"| screenshots | {shots['n']} of ~{shots['expected']} |"]
    (sd / "check.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    if v == "FAIL" and a.move_failed:
        dest_root = sd.parent.parent / "_failed" / sd.parent.name
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / sd.name
        k = 1
        while dest.exists():
            dest = dest_root / f"{sd.name}_{k}"; k += 1
        shutil.move(str(sd), str(dest))
        print(f"FAIL → moved to {dest}")
    return 0 if v == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
