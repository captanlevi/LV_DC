"""check_session.py: post-session acceptance check for collection v2 (LVQoE t35; prereg collection_v2 §2, levi R3/R4).

    python check_session.py <session_dir> [--move-failed] [--max-background-share 0.05]

Writes <session_dir>/check.json and a one-screen check.md. Verdict PASS / FAIL with reasons. With --move-failed a FAIL
moves the session dir to data/_failed/<platform>/ (archive, never delete).

Acceptance is CAPTURE INTEGRITY ONLY (prereg §2, brad R1): nothing that co-varies with impairment filters a session.
  ticks      res_meta present in >= 0.95 of the session's seconds (integrity). `current_time` advancing in >= 0.8 of
             playing ticks is a documented deviation from the prereg's 0.95: it couples to stall detection, and a strict
             value would select on impairment (levi R3c).
  usable     non-ad labelled time, play OR stall, >= 0.6 of the session length (ads by the sensor_v2 span rule)
  shaper     measured downstream <= 1.1 x the set rate in >= 0.9 of episodes
  background downstream bytes on flows whose host (TLS / QUIC SNI, else DNS answers) is not the platform's media host
             <= 0.05 of all downstream bytes; independent of the keylog (a keylog-based share would select clean
             sessions, t23 / t30). The per-episode maximum and the http_v2-based share are reported, not enforced.
  artefacts  no minute with zero client packets while the shaper runs; screenshots present; meta.json complete
  keylog     recorded, never filtered
QA fields for the readings (levi R4): per-signal ad tick counts and ad spans per signal set; Addendum D (twitch: share
of tier changes with a src_changed within ± 2 s; swap-only ad spans, must be 0); Addendum C (realised dwell per shaper
state, in episodes); yt 144 / 240 / 360p shares of quality time (ads masked); ad-action summary; the player pin.
"""
from __future__ import annotations

import argparse
import collections
import csv
import ipaddress
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

CHECKS = {
    "tick_presence_min": 0.95,
    "advancing_share_min": 0.8,
    "usable_share_min": 0.6,
    "shaper_ratio_max": 1.1,
    "shaper_episode_share_min": 0.9,
    "max_background_share": 0.05,
    "shots_share_min": 0.5,
    "shot_interval_s": 10.0,
}
# sensor_v2's span-type ad signals (LVQoE label_policies.SPAN_TYPE_SIGNALS) and its span rule
SPAN_TYPE_SIGNALS = ("duration_finite", "yt_ad_showing", "yt_ad_interrupting", "yt_ad_dom", "tw_ad_dom")
AD_MERGE_GAP_S, AD_PAD_S, SWAP_BOUNDARY_S = 2.0, 1.0, 2.0
# media hosts per platform (LVQoE watcher/lv/constants.py MEDIA_SNI_MP, selection generation 2)
MEDIA_HOSTS = {
    "youtube": [r"(^|\.)googlevideo\.com$"],
    "twitch": [r"(^|\.)ttvnw\.net$", r"(^|\.)live-video\.net$"],
    "tiktok": [r"(^|\.)pull-[a-z0-9-]+\.(tiktokcdn|ttlivecdn)\.com$"],
    "bilibili": [r"(^|\.)bilivideo\.(com|cn)$", r"(^|\.)hdslb\.com$"],
}
MEDIA_URL_PATTERNS = ("/videoplayback", ".ts", ".m4s", ".flv", "/v1/segment/")
META_REQUIRED = ("lvdc_commit", "extension_version", "chrome", "shaper", "channel_url", "hostname", "ntp_offset_start")
TSHARK = shutil.which("tshark") or "tshark"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _true(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "1.0")


# ---------------------------------------------------------------- labels

def read_labels(sd: Path) -> list[dict]:
    p = sd / "labels.csv"
    if not p.exists():
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def ad_spans(meta: list[dict], signal_set=SPAN_TYPE_SIGNALS) -> tuple[list[list[float]], int]:
    """sensor_v2's rule on res_meta ticks: span-type ticks (+ src_changed within 2 s of one), merged across gaps
    <= 2 s, padded ± 1 s. Returns (spans in seconds, number of spans with no span-type tick = swap-only)."""
    allowed = set(signal_set)
    ad_t = sorted(_f(r["ts"]) / 1000 for r in meta if allowed & set((r.get("ad_signals") or "").split("|")))
    sw_t = [_f(r["ts"]) / 1000 for r in meta if _true(r.get("src_changed"))]
    pts = sorted(ad_t + [t for t in sw_t if ad_t and min(abs(t - a) for a in ad_t) <= SWAP_BOUNDARY_S])
    spans: list[list[float]] = []
    for t in pts:
        if spans and t - spans[-1][1] <= AD_MERGE_GAP_S:
            spans[-1][1] = t
        else:
            spans.append([t, t])
    swap_only = sum(1 for lo, hi in spans if not any(lo <= a <= hi for a in ad_t))
    return [[lo - AD_PAD_S, hi + AD_PAD_S] for lo, hi in spans], swap_only


def label_checks(rows: list[dict], platform: str) -> dict:
    meta = [r for r in rows if r.get("type") == "res_meta"]
    if len(meta) < 2:
        return {"n_res_meta": len(meta)}
    ts = [_f(r["ts"]) / 1000.0 for r in meta]
    t0, t1 = min(ts), max(ts)
    dur = t1 - t0
    seconds = max(1, int(dur) + 1)
    present = len({int(t - t0) for t in ts})
    has_v2 = "ad_signals" in meta[0]
    spans, swap_only = ad_spans(meta) if has_v2 else ([], 0)
    in_ad = [any(lo <= t <= hi for lo, hi in spans) for t in ts]
    # stall state from the event stream
    stalled, state = [], False
    for r in rows:
        if r.get("type") == "stall":
            state = True
        elif r.get("type") in ("start", "video_start"):
            state = False
        elif r.get("type") == "res_meta":
            stalled.append(state)
    h = [int(_f(r.get("video_h")) or 0) for r in meta]
    ct = [_f(r.get("current_time")) for r in meta]
    adv = [b is not None and a is not None and b > a for a, b in zip(ct, ct[1:])]
    adv_play = [a for a, s, ad in zip(adv, stalled[1:], in_ad[1:]) if not s and not ad]
    usable = sum(1 for hh, s, ad in zip(h, stalled, in_ad) if not ad and (s or hh > 0))
    tiers = [hh for hh, s, ad in zip(h, stalled, in_ad) if not ad and not s and hh > 0]
    mix = collections.Counter(tiers)
    nq = max(1, len(tiers))
    changes = [(t, a, b) for t, (a, b) in zip(ts[1:], zip(tiers, tiers[1:]))]  # approximate times (dense ticks)
    # Addendum D (a): tier changes with a src_changed within ± 2 s (non-ad play ticks, consecutive)
    play_idx = [i for i, (hh, s, ad) in enumerate(zip(h, stalled, in_ad)) if not ad and not s and hh > 0]
    sw_t = [t for t, r in zip(ts, meta) if _true(r.get("src_changed"))]
    tier_changes = [ts[j] for i, j in zip(play_idx, play_idx[1:]) if j == i + 1 and h[i] != h[j]]
    near = sum(1 for t in tier_changes if any(abs(t - s) <= SWAP_BOUNDARY_S for s in sw_t))
    per_signal = collections.Counter(s for r in meta for s in (r.get("ad_signals") or "").split("|") if s)
    out = {
        "n_res_meta": len(meta), "duration_s": dur,
        "tick_presence": present / seconds,
        "advancing_share_while_playing": (sum(adv_play) / len(adv_play)) if adv_play else None,
        "v2_columns": has_v2,
        "usable_share": usable / seconds,
        "ad_ticks_per_signal": dict(per_signal),
        "ad_spans_span_type_set": len(spans), "ad_span_seconds": sum(hi - lo for lo, hi in spans),
        "ad_spans_per_signal": {s: len(ad_spans(meta, (s,))[0]) for s in SPAN_TYPE_SIGNALS} if has_v2 else {},
        "swap_only_ad_spans": swap_only,
        "src_changed_ticks": len(sw_t),
        "stall_share": sum(stalled) / len(stalled) if stalled else None,
        "tier_mix_quality_time": {str(k): v / nq for k, v in sorted(mix.items())},
        "tier_changes": len(tier_changes),
        "tier_changes_per_min": len(tier_changes) / (dur / 60.0) if dur > 0 else None,
        "tier_changes_with_swap_within_2s_share": (near / len(tier_changes)) if tier_changes else None,
    }
    if platform == "youtube":
        out["yt_low_tier_shares"] = {f"{q}p": mix.get(q, 0) / nq for q in (144, 240, 360)}
    return out


# ---------------------------------------------------------------- traffic

def packets(pcap: Path):
    """Per packet: time, src, dst, sport, dport, proto, length, sni, dns names, dns addresses (tshark fields)."""
    cmd = [TSHARK, "-r", str(pcap), "-T", "fields", "-E", "separator=\t", "-E", "aggregator=|",
           "-e", "frame.time_epoch", "-e", "ip.src", "-e", "ip.dst", "-e", "ipv6.src", "-e", "ipv6.dst",
           "-e", "tcp.srcport", "-e", "tcp.dstport", "-e", "udp.srcport", "-e", "udp.dstport", "-e", "frame.len",
           "-e", "tls.handshake.extensions_server_name", "-e", "dns.qry.name", "-e", "dns.a", "-e", "dns.aaaa"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in proc.stdout:
        f = line.rstrip("\n").split("\t")
        f += [""] * (14 - len(f))
        t, ln = _f(f[0]), _f(f[9])
        src, dst = (f[1] or f[3]), (f[2] or f[4])
        if f[5]:
            sp, dp, pr = f[5], f[6], "TCP"
        elif f[7]:
            sp, dp, pr = f[7], f[8], "UDP"
        else:
            continue
        if t is None or ln is None or not src or not dst:
            continue
        yield (t, src, dst, sp, dp, pr, ln, f[10], f[11], f[12] + ("|" + f[13] if f[13] else ""))
    proc.wait()


def client_ip(pkts) -> str | None:
    c = collections.Counter()
    for p in pkts:
        for ip in (p[1], p[2]):
            try:
                if ipaddress.ip_address(ip).is_private:
                    c[ip] += 1
            except ValueError:
                pass
    return c.most_common(1)[0][0] if c else None


def http_media_flows(sd: Path) -> set:
    p = sd / "http_v2.json"
    if not p.exists() or p.stat().st_size < 10:
        return set()
    keys = set()
    for key, recs in json.load(open(p)).items():
        if any(any(m in (r.get("request_url") or "").split("?")[0] for m in MEDIA_URL_PATTERNS)
               for r in (recs if isinstance(recs, list) else [])):
            try:
                a, b = key.split("->")
                sip, sport = a.split("$")
                dip, dport, proto = b.split("$")
                keys.add((sip, sport, dip, dport, proto))
            except ValueError:
                pass
    return keys


def traffic_checks(sd: Path, platform: str) -> dict:
    pcap = sd / "merged.pcap"
    if not pcap.exists():
        return {"pcap": False}
    pk = list(packets(pcap))
    if not pk:
        return {"pcap": True, "packets": 0}
    me = client_ip(pk)
    t0, t1 = min(p[0] for p in pk), max(p[0] for p in pk)
    # host per flow: SNI on the client's handshake, else DNS answers for the server address
    ip_names: dict[str, set] = collections.defaultdict(set)
    for p in pk:
        if p[8] and p[9]:
            for addr in p[9].split("|"):
                if addr:
                    for name in p[8].split("|"):
                        ip_names[addr].add(name.lower())
    flow_sni: dict[tuple, str] = {}
    for p in pk:
        if p[7] and p[1] == me:
            flow_sni[(p[1], p[3], p[2], p[4], p[5])] = p[7].split("|")[0].lower()
    pats = [re.compile(x) for x in MEDIA_HOSTS.get(platform, [])]

    def is_media(server, key):
        names = {flow_sni[key]} if key in flow_sni else ip_names.get(server, set())
        return any(pt.search(n) for n in names for pt in pats)

    media_cache: dict[tuple, bool] = {}
    down = []
    for p in pk:
        if p[2] != me:
            continue
        key = (p[2], p[4], p[1], p[3], p[5])                  # client-side 5-tuple of the flow
        if key not in media_cache:
            media_cache[key] = is_media(p[1], key)
        down.append((p[0], p[6], media_cache[key], key))
    down_total = sum(d[1] for d in down)
    down_media = sum(d[1] for d in down if d[2])
    http_media = http_media_flows(sd)
    down_http_media = sum(d[1] for d in down if d[3] in http_media)
    # per net_labels episode: shaper binding and background share
    eps, binds = [], []
    nl = sd / "net_labels.txt"
    lab = [json.loads(l) for l in nl.read_text().splitlines() if l.strip()] if nl.exists() else []
    for a, b in zip(lab, lab[1:] + [{"timestamp": t1}]):
        lo, hi = a["timestamp"], b["timestamp"]
        if hi - lo < 10:
            continue
        seg = [d for d in down if lo <= d[0] < hi]
        byts = sum(d[1] for d in seg)
        kbit = byts * 8 / 1000 / (hi - lo)
        ratio = kbit / a["speed"] if a.get("speed") else None
        eps.append({"state": a.get("state"), "set_kbit": a.get("speed"), "measured_kbit": kbit, "ratio": ratio,
                    "background_share": (1 - sum(d[1] for d in seg if d[2]) / byts) if byts else None})
        binds.append(ratio is not None and ratio <= CHECKS["shaper_ratio_max"])
    # capture gaps: minutes with zero client packets while the shaper runs
    gaps = []
    if lab:
        s0, s1 = lab[0]["timestamp"], t1
        have = {int((p[0] - s0) // 60) for p in pk if s0 <= p[0] <= s1 and (p[1] == me or p[2] == me)}
        gaps = [m for m in range(int((s1 - s0) // 60)) if m not in have]
    # Addendum C: realised dwell per state (consecutive episodes)
    dwell = collections.defaultdict(list)
    run_state, run_len = None, 0
    for a in lab:
        st = a.get("state")
        if st == run_state:
            run_len += 1
        else:
            if run_state is not None:
                dwell[run_state].append(run_len)
            run_state, run_len = st, 1
    if run_state is not None:
        dwell[run_state].append(run_len)
    bg_eps = [e["background_share"] for e in eps if e["background_share"] is not None]
    return {
        "pcap": True, "pcap_bytes": pcap.stat().st_size, "minutes": (t1 - t0) / 60.0, "client_ip": me,
        "ssl_keys": (sd / "ssl_keys.log").exists(),
        "background_share": (1 - down_media / down_total) if down_total else None,
        "background_share_episode_max": max(bg_eps) if bg_eps else None,
        "background_share_http_v2": (1 - down_http_media / down_total) if down_total and http_media else None,
        "flows_by_sni": sum(1 for k in media_cache if k in flow_sni), "flows": len(media_cache),
        "episodes": eps, "shaper_bind_share": (sum(binds) / len(binds)) if binds else None,
        "capture_gap_minutes": gaps,
        "dwell_episodes_per_state": {k: v for k, v in sorted(dwell.items(), key=lambda kv: int(kv[0]))},
    }


# ---------------------------------------------------------------- verdict

def meta_check(sd: Path) -> tuple[dict, list[str]]:
    p = sd / "meta.json"
    if not p.exists():
        return {}, ["meta.json missing"]
    m = json.loads(p.read_text())
    missing = [k for k in META_REQUIRED if m.get(k) in (None, "", {}, [])]
    if isinstance(m.get("chrome"), dict) and not m["chrome"].get("browser"):
        missing.append("chrome.browser")
    if isinstance(m.get("lvdc_commit"), dict) and not m["lvdc_commit"].get("sha"):
        missing.append("lvdc_commit.sha")
    if isinstance(m.get("ntp_offset_start"), dict) and not m["ntp_offset_start"].get("raw"):
        missing.append("ntp_offset_start.raw")
    if m.get("platform") == "youtube":
        # t38 / G5 (levi R1, R2): the matrix id, the low-state cap, and the channel handle / type from the list
        sh = m.get("shaper") or {}
        missing += [f"shaper.{k}" for k in ("matrix_id", "low_state_cap") if sh.get(k) in (None, "")]
        missing += [k for k in ("channel_handle", "channel_type") if m.get(k) in (None, "")]
    return m, missing


def verdict(lab: dict, net: dict, shots: dict, meta_missing: list[str], max_bg: float) -> tuple[str, list[str]]:
    why = []
    if not net.get("pcap") or not net.get("packets", 1):
        why.append("no pcap / no packets")
    if net.get("pcap") and not net.get("ssl_keys"):
        why.append("ssl_keys.log missing (v2 keeps the keys)")
    if net.get("capture_gap_minutes"):
        why.append(f"capture gap: minutes with zero client packets {net['capture_gap_minutes']}")
    if not lab.get("n_res_meta"):
        why.append("no res_meta ticks")
    else:
        if lab["tick_presence"] < CHECKS["tick_presence_min"]:
            why.append(f"res_meta ticks in {lab['tick_presence']:.3f} of seconds < {CHECKS['tick_presence_min']}")
        a = lab.get("advancing_share_while_playing")
        if a is not None and a < CHECKS["advancing_share_min"]:
            why.append(f"current_time advancing in {a:.2f} of playing ticks < {CHECKS['advancing_share_min']}")
        if not lab.get("v2_columns"):
            why.append("labels.csv lacks the v2 columns (old extension loaded?)")
        if lab.get("usable_share") is not None and lab["usable_share"] < CHECKS["usable_share_min"]:
            why.append(f"usable (non-ad play or stall) {lab['usable_share']:.2f} of the session < {CHECKS['usable_share_min']}")
    sb = net.get("shaper_bind_share")
    if sb is not None and sb < CHECKS["shaper_episode_share_min"]:
        why.append(f"shaper binds in {sb:.2f} of episodes < {CHECKS['shaper_episode_share_min']}")
    bg = net.get("background_share")
    if bg is not None and bg > max_bg:
        why.append(f"background traffic (by host) {bg:.3f} > {max_bg}")
    if shots.get("share") is not None and shots["share"] < CHECKS["shots_share_min"]:
        why.append(f"screenshots {shots['n']} of ~{shots['expected']} expected")
    if meta_missing:
        why.append(f"meta.json incomplete: {meta_missing}")
    return ("FAIL" if why else "PASS"), why


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--move-failed", action="store_true")
    ap.add_argument("--max-background-share", type=float, default=CHECKS["max_background_share"])
    a = ap.parse_args()
    sd = a.session_dir.resolve()
    meta, meta_missing = meta_check(sd)
    platform = meta.get("platform") or sd.parent.name
    lab = label_checks(read_labels(sd), platform)
    net = traffic_checks(sd, platform)
    shot_files = list((sd / "shots").glob("*.jpg")) if (sd / "shots").exists() else []
    dur = lab.get("duration_s") or (net.get("minutes") or 0) * 60
    exp_n = int(dur / CHECKS["shot_interval_s"]) if dur else 0
    shots = {"n": len(shot_files), "expected": exp_n, "share": (len(shot_files) / exp_n) if exp_n else None}
    acts = collections.Counter()
    if (sd / "ad_actions.jsonl").exists():
        for l in (sd / "ad_actions.jsonl").read_text().splitlines():
            if l.strip():
                acts[json.loads(l)["action"].split(" [")[0]] += 1
    pin = meta.get("player_pin") or {}
    v, why = verdict(lab, net, shots, meta_missing, a.max_background_share)
    out = {"session": str(sd), "platform": platform, "verdict": v, "reasons": why, "thresholds": CHECKS,
           "max_background_share": a.max_background_share, "labels": lab, "traffic": net, "shots": shots,
           "ad_actions": dict(acts), "player_pin": pin, "meta_missing": meta_missing,
           "matrix": (meta.get("shaper") or {}).get("matrix_id"), "low_state_cap": (meta.get("shaper") or {}).get("low_state_cap")}
    (sd / "check.json").write_text(json.dumps(out, indent=1, default=str))
    md = [f"# check: {platform}/{sd.name} — **{v}**", ""] + [f"- {r}" for r in why] + ["",
          "| item | value |", "|---|---|",
          f"| duration (min) | {dur / 60:.1f} |" if dur else "| duration | — |",
          f"| res_meta tick presence (share of seconds) | {lab.get('tick_presence')} |",
          f"| current_time advancing (playing, non-ad) | {lab.get('advancing_share_while_playing')} |",
          f"| usable share (non-ad play or stall) | {lab.get('usable_share')} |",
          f"| ad ticks per signal | {lab.get('ad_ticks_per_signal')} |",
          f"| ad spans (span-type set) / seconds / per signal | {lab.get('ad_spans_span_type_set')} / {lab.get('ad_span_seconds')} / {lab.get('ad_spans_per_signal')} |",
          f"| Addendum D: swap-only ad spans (must be 0) | {lab.get('swap_only_ad_spans')} |",
          f"| Addendum D: tier changes with src_changed within ± 2 s | {lab.get('tier_changes_with_swap_within_2s_share')} (of {lab.get('tier_changes')}) |",
          f"| stall share | {lab.get('stall_share')} |",
          f"| tier mix (quality time, ads masked) | {lab.get('tier_mix_quality_time')} |",
          f"| yt 144 / 240 / 360p shares | {lab.get('yt_low_tier_shares')} |",
          f"| tier changes / min | {lab.get('tier_changes_per_min')} |",
          f"| background share by host (episode max) / by http_v2 | {net.get('background_share')} ({net.get('background_share_episode_max')}) / {net.get('background_share_http_v2')} |",
          f"| shaper binds (share of episodes) | {net.get('shaper_bind_share')} |",
          f"| Addendum C: dwell per state (episodes in a row) | {net.get('dwell_episodes_per_state')} |",
          f"| matrix / low-state cap | {out['matrix']} / {out['low_state_cap']} |",
          f"| channel (handle / type) | {meta.get('channel_handle')} / {meta.get('channel_type')} |",
          f"| capture gap minutes | {net.get('capture_gap_minutes')} |",
          f"| ssl keys kept | {net.get('ssl_keys')} |",
          f"| screenshots | {shots['n']} of ~{shots['expected']} |",
          f"| ad actions | {dict(acts)} |",
          f"| player pin: target / realised / ok | {pin.get('target')} / {pin.get('final_player')} / {pin.get('ok')} |",
          f"| meta.json missing | {meta_missing} |"]
    (sd / "check.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    if v == "FAIL" and a.move_failed:
        dest_root = sd.parent.parent / "_failed" / sd.parent.name
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / sd.name
        k = 1
        while dest.exists():
            dest = dest_root / f"{sd.name}_{k}"
            k += 1
        shutil.move(str(sd), str(dest))
        print(f"FAIL → moved to {dest}")
    return 0 if v == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
