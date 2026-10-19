#!/usr/bin/env python3
"""t53 gate 2, items 4a and 8 (levi's checklist, LVQoE docs/analysis/design/t53_pilot_gate2_checklist_levi_2026-10-19.md)
from a session's labels.csv and shots/index.jsonl, for shagun's per-session gate-2 json, in both modes (the trio session
and the single-mode reference read the same way). Read-only; no media truth.

Checklist as revised by levi's gate-2 call on pilot-1 (2026-10-19, from round 1):
An item not recorded is a FAIL (ok false, recorded false; levi); ok null only where a rule says "reported" (4a
insufficient support, 5 not shaper-bound, 1 / 2 content band only). Admission: no gated ok is false.
  1 / 2 verify / check: PASS; a non-PASS whose every reason is a content band (verify's B/s-per-tier p10..p90; the speed
     rule is a verify note already) is reported and the session handled as in single mode; any other reason gates.
  4a visibility: every res_meta tick's document.visibilityState is `visible` (labels.csv `visibility`; a session from
     before the column: "not recorded"). Rendered frames per media second while playing, per tier (labels.csv
     `total_frames` steps over the playhead's advance, shot-free; the tier from the ticks' short side) >=
     RENDER_FPS_MIN_SHARE x the frames per media second the source delivered (LVQoE scripts/audit/rendition_fps.py
     delivered_fps; --rendition-fps), on >= MIN_SUPPORT_S shot-free playing seconds, else "insufficient support"; no
     delivered fps: not read. fps60_covered: a tier delivered at >= 50 fps read on enough support (twitch's 60 fps tiers
     must be covered before twitch passes).
  5  shaping fidelity, reference-free (trio sessions: <session>/tc_stats.jsonl): per shaper state, over the seconds the
     slot's ingress class was shaper-bound (backlog, or drops grew: trio_slots' own rule), the byte-total ratio
     sum(class bytes) x 8 / sum(class rate x seconds) in [ITEM5_LO, ITEM5_HI]; below: FAIL (starvation while queued);
     above: reported; fewer than MIN_SUPPORT_S bound seconds: "not shaper-bound". A byte total is invariant to coalescing.
  7 / 9 / teardown (trio, meta.json concurrency): flagged link seconds <= LINK_FLAG_MAX_SHARE; no kernel capture drop;
     the slot came down (nothing left in staging; slot_down's only failures are "not found" deletes of filters
     slot_detach removed with rc 0).
  11-14 (trio, flow_overlap.py's run json, --flow-overlap; levi's call on pilot-1): 11 no unicast flow or QUIC CID
     shared with a sibling, link-local multicast (each Chrome's own mDNS) reported when packet-disjoint, no
     payload-carrying packet of the session's flows in the default class (control-only splits reported; needs
     flow_overlap --detail, else not read); 12 no sibling platform's host family; 13 no media of the session's platform
     in the default class during it; 14 every contacted media address named in dns_answers.json.
  8  (reported, never gating) dropped frames per playing second outside +- SHOT_GUARD_S of any shot capture (every shots/index.jsonl row's
     [t_decide, ts], any reason), in both modes (the shot-free rule keeps it comparable when trio shots are CDP captures).
A playing interval: two consecutive res_meta ticks, both not paused and not ad, the gap <= MAX_GAP_S and the playhead
advancing between them. Counter steps are clamped at 0 (a new <video> element resets the counters).

    python3 gate2_items.py <session dir> ... [--rendition-fps fps.json] [--out items.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coverage_report import quality_class  # noqa: E402

SHOT_GUARD_S = 2.0              # item 8 (levi 2026-10-19): +- 2 s of any shot capture
RENDER_FPS_MIN_SHARE = 0.9      # item 4a (levi's call on pilot-1): rendered >= 0.9 x delivered frames per media second
MIN_SUPPORT_S = 10.0            # item 4a's shot-free playing seconds per tier, item 5's shaper-bound seconds per state
ITEM5_LO, ITEM5_HI = 0.9, 1.15
TC_PARTIAL_MAX_SHARE = 0.1      # item 5 (levi): more partial tc samples than this share: "tc recording incomplete", a FAIL  # item 5 (levi): the byte-total ratio over shaper-bound seconds
MAX_GAP_S = 3.0                 # a tick gap above this is not a playing interval (a stalled poll, a navigation)


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _true(x) -> bool:
    return str(x).strip().lower() == "true"


def ticks(sd: Path) -> list[dict]:
    """labels.csv res_meta rows: t (s), tier, playing state and the counters."""
    out = []
    with open(Path(sd) / "labels.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("type") != "res_meta":
                continue
            t = _f(r.get("ts"))
            if t is None:
                continue
            w, h = _f(r.get("video_w")), _f(r.get("video_h"))
            out.append({"t": t / 1000.0, "tier": quality_class(w, h) if w and h else None,
                        "paused": _true(r.get("paused")), "ad": _true(r.get("is_ad")), "ct": _f(r.get("current_time")),
                        "dropped": _f(r.get("dropped")), "total": _f(r.get("total_frames")),
                        "visibility": r.get("visibility") if "visibility" in r else None,
                        "has_visibility": "visibility" in r, "has_total": "total_frames" in r})
    return sorted(out, key=lambda x: x["t"])


def shot_spans(sd: Path) -> list[tuple[float, float]]:
    """Every shot capture's [t_decide, ts] (s; ts alone when t_decide is absent), from shots/index.jsonl."""
    p = Path(sd) / "shots" / "index.jsonl"
    out = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = _f(r.get("ts"))
            if ts is None:
                continue
            t0 = _f(r.get("t_decide"))
            out.append((min(t0, ts) if t0 is not None else ts, ts))
    return sorted(out)


def intervals(tk: list[dict], spans: list[tuple[float, float]], guard_s: float = SHOT_GUARD_S) -> list[dict]:
    """Playing intervals between consecutive ticks, each with its duration, counter steps, tier (both ends' when equal)
    and whether it is clear of every shot span widened by guard_s."""
    out = []
    for a, b in zip(tk, tk[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0 or dt > MAX_GAP_S or a["paused"] or b["paused"] or a["ad"] or b["ad"]:
            continue
        if a["ct"] is None or b["ct"] is None or not (0 < b["ct"] - a["ct"] <= 2 * dt + 1):
            continue
        near = any(s0 - guard_s < b["t"] and a["t"] < s1 + guard_s for s0, s1 in spans)
        step = (lambda k: None if a[k] is None or b[k] is None else max(0.0, b[k] - a[k]))
        out.append({"t0": a["t"], "dt": dt, "dct": b["ct"] - a["ct"], "dropped": step("dropped"), "total": step("total"),
                    "tier": a["tier"] if a["tier"] == b["tier"] else None, "shot_free": not near})
    return out


def item8(iv: list[dict], n_shots: int) -> dict:
    def rate(xs):
        xs = [x for x in xs if x["dropped"] is not None]
        s = sum(x["dt"] for x in xs)
        return (round(sum(x["dropped"] for x in xs) / s, 4) if s else None), round(s, 1), int(sum(x["dropped"] for x in xs))
    r_free, s_free, d_free = rate([x for x in iv if x["shot_free"]])
    r_all, s_all, d_all = rate(iv)
    return {"dropped_per_playing_s_shot_free": r_free, "playing_s_shot_free": s_free, "dropped_shot_free": d_free,
            "dropped_per_playing_s_all": r_all, "playing_s_all": s_all, "dropped_all": d_all,
            "shots": n_shots, "guard_s": SHOT_GUARD_S}


def item4a(tk: list[dict], iv: list[dict], rendition_fps: dict | None = None) -> dict:
    """Item 4a (module docstring). rendition_fps: the session's rendition_fps.py detail, {tier: {delivered_fps, ...}}."""
    if not tk or not tk[0]["has_visibility"]:
        vis = {"recorded": False, "ok": False}                 # levi: in gate 2 an item not recorded is a FAIL
    else:
        bad = [x for x in tk if x["visibility"] != "visible"]
        vals: dict = {}
        for x in tk:
            vals[x["visibility"]] = vals.get(x["visibility"], 0) + 1
        vis = {"recorded": True, "samples": len(tk), "not_visible": len(bad), "values": vals,
               "first_not_visible_t": bad[0]["t"] if bad else None, "ok": not bad}
    fps: dict = {}
    if tk and tk[0]["has_total"]:
        for tier in sorted({x["tier"] for x in iv if x["tier"] and x["total"] is not None}):
            xs = [x for x in iv if x["tier"] == tier and x["total"] is not None]
            fr = [x for x in xs if x["shot_free"]]
            s_all, s_free = sum(x["dt"] for x in xs), sum(x["dt"] for x in fr)
            m_free = sum(x["dct"] for x in fr)
            tot_free, drop_free = sum(x["total"] for x in fr), sum(x["dropped"] or 0.0 for x in fr)
            per_media = round(tot_free / m_free, 2) if m_free > 0 else None
            ref = ((rendition_fps or {}).get(tier) or {}).get("delivered_fps")
            if s_free < MIN_SUPPORT_S:
                ok, note = None, "insufficient support"
            elif ref is None or per_media is None:
                ok, note = False, "no delivered fps (not recorded: a FAIL)"
            else:
                ok, note = per_media >= RENDER_FPS_MIN_SHARE * ref, None
            fps[tier] = {"fps_per_media_s_shot_free": per_media, "delivered_fps": ref,
                         "ratio": round(per_media / ref, 3) if per_media is not None and ref else None,
                         "playing_s_shot_free": round(s_free, 1), "min_share": RENDER_FPS_MIN_SHARE,
                         "min_support_s": MIN_SUPPORT_S, "ok": ok, **({"note": note} if note else {}),
                         # reported beside (pilot-1's decomposition): per wall second, the playback rate, the dropped share
                         "fps_shot_free": round(tot_free / s_free, 2) if s_free else None,
                         "fps_all": round(sum(x["total"] for x in xs) / s_all, 2) if s_all else None,
                         "playing_s_all": round(s_all, 1),
                         "playback_rate_shot_free": round(m_free / s_free, 3) if s_free else None,
                         "dropped_share_shot_free": round(drop_free / tot_free, 3) if tot_free else None}
    read = [v["ok"] for v in fps.values() if v.get("note") != "insufficient support"]
    oks = [vis["ok"]] + read + ([] if tk and tk[0]["has_total"] else [False])
    fps60 = (None if not fps else any((v.get("delivered_fps") or 0) >= 50 and v.get("note") != "insufficient support"
                                      for v in fps.values()))
    return {"visibility": vis, "rendered_fps": fps if tk and tk[0]["has_total"] else {"recorded": False},
            "fps60_covered": fps60, "ok": False if any(o is False for o in oks) else (None if any(o is None for o in oks) else True)}


def item5(sd: Path) -> dict:
    """Item 5, reference-free (module docstring): per shaper state over the slot class's shaper-bound seconds."""
    import bisect
    p = Path(sd) / "tc_stats.jsonl"
    if not p.exists():
        return {"recorded": False, "ok": False}
    # a sample whose class read failed holds the rate only (round 1: 15 / 1246 on yt_100004): skipped, counted
    allrows = [r for r in (json.loads(line) for line in p.read_text().splitlines() if line.strip()) if r.get("in")]
    rows = [r for r in allrows if all(k in r["in"] for k in ("bytes", "drops", "backlog", "rate_kbit"))]
    nl = Path(sd) / "net_labels.txt"
    lab = (sorted((json.loads(line) for line in nl.read_text().splitlines() if line.strip()), key=lambda x: x["timestamp"])
           if nl.exists() else [])
    lt = [x["timestamp"] for x in lab]
    per: dict = {}
    for a, b in zip(rows, rows[1:]):
        dt = b["ts"] - a["ts"]
        ia, ib = a["in"], b["in"]
        db = ib["bytes"] - ia["bytes"]
        if dt <= 0 or db < 0 or ia.get("rate_kbit") != ib.get("rate_kbit") or not ib.get("rate_kbit"):
            continue                                       # a counter reset or a rate change inside the interval
        if not (ib["backlog"] or ib["drops"] > ia["drops"]):
            continue                                       # not shaper-bound (trio_slots' own rule)
        i = bisect.bisect_right(lt, a["ts"]) - 1
        if i < 0 or bisect.bisect_right(lt, b["ts"]) - 1 != i:
            continue                                       # before the first state, or a state change inside
        s = per.setdefault(str(lab[i]["state"]), [0.0, 0.0, 0.0])
        s[0] += db * 8 / 1000.0
        s[1] += ib["rate_kbit"] * dt
        s[2] += dt                                         # levi: bound seconds, whatever the sampling period
    out, oks = {}, []
    for st, (kbit, want, n) in sorted(per.items(), key=lambda x: int(x[0])):
        r = round(kbit / want, 3) if want else None
        if n < MIN_SUPPORT_S:
            out[st] = {"bound_s": round(n, 1), "ratio": r, "reading": "not shaper-bound", "ok": None}
            continue
        ok = r >= ITEM5_LO
        out[st] = {"bound_s": round(n, 1), "ratio": r, "ok": ok,
                   "reading": "FAIL (starvation while queued)" if not ok else ("above (reported)" if r > ITEM5_HI else "PASS")}
        oks.append(ok)
    partial = len(allrows) - len(rows)
    incomplete = bool(allrows) and partial / len(allrows) > TC_PARTIAL_MAX_SHARE      # levi: then not read, a FAIL
    return {"recorded": True, "per_state": out, "band": [ITEM5_LO, ITEM5_HI], "min_bound_s": MIN_SUPPORT_S,
            "samples": len(allrows), "samples_partial_skipped": partial,
            **({"reading": "tc recording incomplete"} if incomplete else {}),
            "ok": False if incomplete else (all(oks) if oks else None)}


LINK_FLAG_MAX_SHARE = 0.05     # item 7: flagged link seconds <= 5 % of the session


def concurrency_items(meta: dict) -> dict:
    """Items 7, 9 and the teardown from meta.json concurrency (trio sessions; {} otherwise)."""
    c = meta.get("concurrency") or {}
    if c.get("mode") != "trio":
        return {}
    tc, cd = c.get("tc") or {}, c.get("capture_drops") or {}
    fs = tc.get("flagged_share")
    detached = {x.get("cmd") for x in c.get("slot_detach") or [] if x.get("rc") == 0}
    bad = [f for f in (c.get("slot_down") or {}).get("failed") or []
           if not (f.get("cmd") in detached and "not found" in str(f.get("err")))]
    dk = cd.get("dropped_kernel_total")
    return {"item7": {"flagged_share": fs, "max": LINK_FLAG_MAX_SHARE, "recorded": fs is not None,
                      "ok": fs is not None and fs <= LINK_FLAG_MAX_SHARE},
            "item9": {"dropped_kernel_total": dk, "recorded": dk is not None, "ok": dk == 0},
            "teardown": {"staging_left": c.get("staging_left"), "slot_down_failures_not_benign": bad,
                         "ok": not c.get("staging_left") and not bad}}


def overlap_items(name: str, fo: dict | None) -> dict:
    """Items 11-14 for session `name` ("<platform>/<session>") from flow_overlap.py's run json; {} without it."""
    if not fo or name not in (fo.get("sessions") or {}):
        return {}
    pairs = [x for x in fo.get("pairs") or [] if name in (x.get("a"), x.get("b"))]
    rec = fo["sessions"][name]
    sd = (fo.get("split_detail") or {}).get(name)
    unicast = sum(x.get("shared_unicast_flows", x.get("shared_flows", 0)) for x in pairs)
    cids = sum(x.get("shared_quic_cids", 0) for x in pairs)
    mc = [m for x in pairs for m in x.get("shared_link_local_multicast") or []]
    mc_both = sum(m.get("packets_in_both", 0) for m in mc)
    payload = None if sd is None else sum(1 for r in sd.get("rows") or [] if r.get("payload_packets", 0) > 0)
    control = None if sd is None else sum(1 for r in sd.get("rows") or [] if not r.get("payload_packets"))
    fail11 = bool(unicast or cids or mc_both or payload)
    unread = sd is None and bool(rec.get("flows_also_in_default"))   # not recorded: a FAIL (levi)
    ok11 = not fail11 and not unread
    item11 = {"shared_unicast_flows": unicast, "shared_quic_cids": cids, "link_local_multicast_flows": len(mc),
              "multicast_packets_in_both": mc_both, "default_split_flows_payload": payload,
              "default_split_flows_control_only": control, "recorded": not unread, "ok": ok11,
              **({"note": "flows also in the default class, not read: run flow_overlap --detail"} if unread else {})}
    fam = rec.get("sibling_family_snis") or {}
    dm, du = rec.get("default_media_snis_in_window") or [], (rec.get("default_udp_from_media_addresses") or {}).get("packets", 0)
    dj = rec.get("dns_answers_json") or {}
    return {"item11": item11, "item12": {"sibling_family_snis": fam, "ok": not fam},
            "item13": {"default_media_snis_in_window": dm, "default_udp_from_media_addresses": du, "ok": not dm and not du},
            "item14": {"present": dj.get("present"), "media_unnamed": dj.get("media_unnamed"), "recorded": bool(dj),
                       "ok": bool(dj) and bool(dj.get("present")) and not dj.get("media_unnamed")}}


def missing_overlap_items() -> dict:
    """A trio session that flow_overlap's json does not cover: items 11-14 not recorded, a FAIL each (levi)."""
    return {k: {"recorded": False, "ok": False, "note": "no flow_overlap record for this session"}
            for k in ("item11", "item12", "item13", "item14")}


FS_FALLBACK_FLAG_SHARE = 0.2    # levi (t53 (a)): a fullscreen yt session's cadence shots on the CDP fallback above this


def fullscreen_report(sd: Path, meta: dict) -> dict | None:
    """t53 (a), per fullscreen-in-trio session (meta.fullscreen_keeper): the keeper's checks / not_on_top / raises and
    the share of cadence shots captured by the CDP fallback (on_top false, or a failed grab); above
    FS_FALLBACK_FLAG_SHARE the session's shot-derived readings (the anchor, screenshot MAD) carry a flag. None otherwise."""
    k = meta.get("fullscreen_keeper")
    if not k:
        return None
    idx = Path(sd) / "shots" / "index.jsonl"
    rows = [json.loads(line) for line in idx.read_text().splitlines() if line.strip()] if idx.exists() else []
    cad = [r for r in rows if r.get("reason") == "cadence"]
    fb = sum(1 for r in cad if r.get("capture") == "cdp_fallback")
    share = round(fb / len(cad), 3) if cad else None
    return {"checks": k.get("checks"), "not_on_top": k.get("not_on_top"),
            "raises": sum(1 for e in k.get("events") or [] if e.get("activate_sent")),
            "cadence_shots": len(cad), "cdp_fallback": fb, "cdp_fallback_share": share,
            "off_top_shots": sum(1 for r in cad if r.get("on_top") is False),
            "shot_readings_flag": bool(share is not None and share > FS_FALLBACK_FLAG_SHARE)}


CONTENT_BAND_PREFIXES = ("B/s outside old p10..p90", "speed rule accuracy")   # levi: content bands, not trio signals


def items1_2(sd: Path) -> dict:
    """Items 1 / 2 (module docstring): verify.json and check.json, each PASS (check: class normal or censored), a
    content-band-only non-PASS (reported, excluded as in single mode) or a gating failure."""
    out = {}
    for item, name in (("1", "verify.json"), ("2", "check.json")):
        p = Path(sd) / name
        if not p.exists():
            out[item] = {"recorded": False, "ok": False, "reading": f"{name} missing"}
            continue
        v = json.loads(p.read_text())
        reasons = list(v.get("reasons") or [])
        passed = v.get("verdict") == "PASS" and (item == "1" or v.get("session_class") in ("normal", "censored"))
        content = [r for r in reasons if str(r).startswith(CONTENT_BAND_PREFIXES)]
        if passed:
            ok, reading = True, "PASS"
        elif reasons and len(content) == len(reasons):
            ok, reading = None, "content band only: reported, the session excluded as in single mode"
        else:
            ok, reading = False, "gates"
        out[item] = {"verdict": v.get("verdict"), "session_class": v.get("session_class"), "reasons": reasons,
                     "content_band_reasons": content, "ok": ok, "reading": reading}
    return out


def session_items(sd: Path, rendition_fps: dict | None = None, flow_overlap: dict | None = None) -> dict:
    sd = Path(sd)
    tk, spans = ticks(sd), shot_spans(sd)
    iv = intervals(tk, spans)
    meta = {}
    try:
        meta = json.loads((sd / "meta.json").read_text())
    except (OSError, ValueError):
        pass
    c = meta.get("concurrency") or {}
    import record
    is_rec = record.is_record(meta)
    return {"session": str(sd), "mode": c.get("mode", "single"),
            # levi: the operator sets the flag; printed so a forgotten flag or a flagged pilot shows in the round summary
            "record": {"flag": c.get("record"), "era": c.get("era"), "is_record": is_rec},
            "ticks": len(tk), "items1_2": items1_2(sd), "item4a": item4a(tk, iv, rendition_fps), "item5": item5(sd),
            "item8": item8(iv, len(spans)), **concurrency_items(meta),
            **(overlap_items(f"{sd.parent.name}/{sd.name}", flow_overlap)
               or (missing_overlap_items() if (meta.get("concurrency") or {}).get("mode") == "trio" else {})),
            **({"fullscreen": fs} if (fs := fullscreen_report(sd, meta)) else {})}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--rendition-fps", type=Path, default=None,
                    help="LVQoE scripts/audit/rendition_fps.py --out json (per session: detail per tier with delivered_fps)")
    ap.add_argument("--flow-overlap", type=Path, action="append", default=[],
                    help="flow_overlap.py --out json of the run (repeatable: one per run); items 11-14")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    fps = json.loads(a.rendition_fps.read_text()) if a.rendition_fps else {}
    fos = [json.loads(f.read_text()) for f in a.flow_overlap]
    rows = []
    for sd in a.sessions:
        try:
            rows.append(session_items(sd, (fps.get(sd.name) or {}).get("detail"),
                                      next((f for f in fos if f"{sd.parent.name}/{sd.name}" in (f.get("sessions") or {})), None)))
        except Exception as e:  # noqa: BLE001 -- one session's failure is reported
            rows.append({"session": str(sd), "error": repr(e)[:300]})
        print(json.dumps(rows[-1]), flush=True)
    if a.out:
        a.out.write_text(json.dumps({"sessions": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
