#!/usr/bin/env python3
"""t56 decode_stress (design v4 §9 (5); rushi's decision A; levi 00271): one N step's decode / CPU / GPU gate. N real
sessions, one per slot of the step's composition, under their own root data/_trio_stress (orchestrate --trio-stress:
never counted, never --trio-record), each slot's shaper held at the top state (exp.py --hold-state) so the platform's
most expensive tier is reachable; the first yt slot fullscreen when (a) is on (--yt-fullscreen), the second never. The
device record (gpu_record.py, under sudo -n) runs for the whole stress. Sessions run their collection length (EPISODES_V2,
above the gate's 10 min).

Read per session (labels.csv, meta.json; read only):
  - stalls: a stall event to the next start / video_start / end. Its cause by buffer-ahead sampled at the event (levi):
    device >= 2.0 s, network < 2.0 s, unattributed when none was recorded (tt's MediaStream player has no element
    buffer); a stall inside an ad or on a 0 x 0 element is a loading stall, reported apart.
  - playing seconds: res_meta ticks not paused, not an ad, playhead advancing (playhead_still_s < 1 s).
  - rendered fps per tier (video_h) from total_frames over playing ticks, and the dropped share: recorded, never a gate
    (decision A). Delivered fps is the post-hoc reader's (LVQoE rendition_fps on these sessions).
  - the landed room / channel against the requested one (tt pins are best effort: TikTok moves the page), and tt's
    60 fps tier: "reached" when a tier rendered >= 50 fps over >= 10 s, else stated "tt 60 fps tier not reached
    (rendered); delivered post hoc", never a failure.
Gate (levi 00271, decision A):
  - per slot: device-stall seconds <= 2 % of its playing seconds; a slot whose stalls all lack a buffer reading is "not
    readable" (tt at N = 3: a stated gap), not a failure;
  - box (the device record over the stress): no swap growth, no OOM kill; CPU p95 above 85 % = "short of headroom"
    (N_collect then steps down once); GPU engine busy is reported (the 85 % line a report, not a gate);
  - every slot ran one session of at least 10 min.
Short form (--short = --minutes 9; LVQoE restart prereg, short dry runs): each held session runs --minutes base episodes
(orchestrate --trio-stress --episodes), the 10 min session rule gives way to the support check, and in either form a
pass below the minimum reads "insufficient", never a pass (dry_minimums.decode_stress); a fail stays a fail. The minimum
(levi, LVQoE prereg 40843f81): >= 5 min with every slot at its top shaping state (net_labels.txt rows of the held
state) and every slot on (levi, prereg 135e8160: the intersection of each slot's on-spans, a consecutive pair of res_meta
ticks at most 2 s apart being on when both are playing or both are an unpaused ad), at whatever tier the ABR holds; each
slot reports its seconds per tier over those seconds (a slot never reaching its top tier there is stated); CPU p95 is
read over the same seconds (the union of the window's spans), swap growth and OOM kills (counters) over the whole
stress. --reread <report.json> re-reads a
finished stress's sessions and device record with it.
The null comes first and is not this script's: the same device-stall definition on the frozen single-mode reference
sessions (p90 above 2 % stops the gate), the readers' (brad / levi); --read-only reads any session dirs with it.

    python3 ops/diag/decode_stress.py --slots youtube=2,twitch=1,tiktok=1 --out <dir> [--yt-fullscreen] [--dpms-off]
    python3 ops/diag/decode_stress.py --read-only <session_dir> ...        (the readings alone, no run)
    python3 ops/diag/decode_stress.py --reread <out>/report.json [--short]  (a finished stress re-gated; report_reread.json)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dry_minimums as M  # noqa: E402  (restart prereg: the short dry runs' minimum support per gate)

REPO = Path(__file__).resolve().parents[2]
NS = REPO / "networkSim"
DATA = REPO / "data"
STRESS = "_trio_stress"
DEVICE_STALL_BA_S = 2.0            # levi 00271: a stall with >= 2 s ahead is not an underrun on any platform
DEVICE_STALL_SHARE_MAX = 0.02
CPU_P95_MAX = 85.0
TOP_STATE = "10"                   # utils/shaping.SCENARIOS' highest (6000-9000 kbit)
MIN_SESSION_S = 600.0
TIER_MIN_S = 10.0
FPS60_MIN = 50.0
PLATFORMS = ("youtube", "twitch", "tiktok")
# levi (shagun's 7de5518 cut): a cut stress tears down its whole tree, and a new one never starts beside a live one.
# orchestrate runs in its own session (process group); decode_stress's SIGTERM / SIGINT / SIGHUP is forwarded to it as
# SIGTERM (its own clean drain: sessions finalise, slots and the tree come down); after STOP_GRACE_S its group is
# killed; the device recorder is always stopped (stop_recorder: sudo kill TERM, then KILL). TREE_FILE records the
# live tree's pids; a launch is refused while it names a live pid or any orchestrate --trio-stress runs.
STOP_GRACE_S = 900.0               # = orchestrate's own wait for a session to finalise
TREE_FILE = DATA / STRESS / "_summaries" / "decode_stress_tree.json"


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _b(x) -> bool | None:
    return {"True": True, "true": True, "False": False, "false": False}.get(str(x))


def read_labels(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def stalls(rows: list[dict], t_end: float | None = None) -> list[dict]:
    """Each stall event to the next start / video_start / end (seconds), with buffer-ahead at the event and its cause:
    device / network / unattributed (no buffer-ahead recorded). A stall inside an ad or on a 0 x 0 element (a fresh
    element loading: yt's ad / content swaps, levi on yt_900001) is a loading stall, cause "load", never a playback
    reading."""
    ev = [(float(r["ts"]) / 1000.0, r["type"], _f(r.get("buffered_ahead_s")),
           _b(r.get("is_ad")) is True or (_f(r.get("video_h")) or 0) <= 0) for r in rows if _f(r.get("ts")) is not None]
    out = []
    for i, (t, typ, ba, load) in enumerate(ev):
        if typ != "stall":
            continue
        t1 = next((u for u, ty, _, _ in ev[i + 1:] if ty in ("start", "video_start", "end")), t_end if t_end else ev[-1][0])
        cause = ("load" if load else "unattributed" if ba is None else
                 "device" if ba >= DEVICE_STALL_BA_S else "network")
        out.append({"t0": round(t, 3), "t1": round(t1, 3), "s": round(max(t1 - t, 0.0), 3), "ba": ba, "cause": cause})
    return out


def _playing(r: dict) -> bool:
    if r.get("type") != "res_meta" or _b(r.get("paused")) is True or _b(r.get("is_ad")) is True:
        return False
    still = _f(r.get("playhead_still_s"))
    return still is None or still < 1.0


def _ad_on(r: dict) -> bool:
    return r.get("type") == "res_meta" and _b(r.get("is_ad")) is True and _b(r.get("paused")) is False


def on_spans(rows: list[dict]) -> list[list[float]]:
    """levi (prereg 135e8160): the slot's on seconds, a consecutive pair of res_meta ticks at most 2 s apart being on when
    both are playing or both are an unpaused ad; a stalled, paused or loading stretch is off."""
    ticks = [r for r in rows if r.get("type") == "res_meta" and _f(r.get("ts")) is not None]
    out: list[list[float]] = []
    for a, b in zip(ticks, ticks[1:]):
        ta, tb = float(a["ts"]) / 1000.0, float(b["ts"]) / 1000.0
        if not (0.0 < tb - ta <= 2.0 and ((_playing(a) and _playing(b)) or (_ad_on(a) and _ad_on(b)))):
            continue
        if out and out[-1][1] == ta:
            out[-1][1] = tb
        else:
            out.append([ta, tb])
    return [[round(a, 3), round(b, 3)] for a, b in out]


def playing_seconds(rows: list[dict]) -> float:
    """Playing res_meta ticks, each weighted by its gap to the next tick (capped at 2 s)."""
    ticks = [r for r in rows if r.get("type") == "res_meta" and _f(r.get("ts")) is not None]
    s = 0.0
    for a, b in zip(ticks, ticks[1:]):
        if _playing(a):
            s += min(max(float(b["ts"]) - float(a["ts"]), 0.0) / 1000.0, 2.0)
    return round(s, 3)


def _inside(t: float, window: list[list[float]] | None) -> bool:
    return window is None or any(a <= t < b for a, b in window)


def tiers(rows: list[dict], window: list[list[float]] | None = None) -> dict:
    """Per video height: playing support seconds and rendered fps (total_frames over consecutive playing ticks of that
    height), and the dropped share over the same pairs; with a window ([[t0, t1], ...], seconds) only the pairs starting
    inside it."""
    ticks = [r for r in rows if r.get("type") == "res_meta" and _f(r.get("ts")) is not None]
    agg: dict = {}
    for a, b in zip(ticks, ticks[1:]):
        if not (_playing(a) and _playing(b)) or a.get("video_h") != b.get("video_h"):
            continue
        if not _inside(float(a["ts"]) / 1000.0, window):
            continue
        fa, fb = _f(a.get("total_frames")), _f(b.get("total_frames"))
        dt = (float(b["ts"]) - float(a["ts"])) / 1000.0
        if fa is None or fb is None or not 0 < dt <= 2.0 or fb < fa:
            continue
        g = agg.setdefault(str(int(_f(a.get("video_h")) or 0)), {"s": 0.0, "frames": 0.0, "dropped": 0.0})
        g["s"] += dt
        g["frames"] += fb - fa
        da, db = _f(a.get("dropped")), _f(b.get("dropped"))
        if da is not None and db is not None and db >= da:
            g["dropped"] += db - da
    return {h: {"s": round(g["s"], 1), "rendered_fps": round(g["frames"] / g["s"], 2) if g["s"] else None,
                "dropped_share": round(g["dropped"] / g["frames"], 4) if g["frames"] else None}
            for h, g in sorted(agg.items(), key=lambda kv: int(kv[0]))}


def top_state_spans(sd: Path, state: str, t_end: float | None) -> list[list[float]]:
    """The spans a session's shaper sat at `state`: each net_labels.txt row (one per tc change) holds its state to the
    next row (the last to t_end); adjacent spans merged."""
    try:
        rows = [json.loads(ln) for ln in (Path(sd) / "net_labels.txt").read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []
    rows = sorted((r for r in rows if _f(r.get("timestamp")) is not None), key=lambda r: float(r["timestamp"]))
    out: list[list[float]] = []
    for i, r in enumerate(rows):
        a = float(r["timestamp"])
        b = float(rows[i + 1]["timestamp"]) if i + 1 < len(rows) else t_end
        if str(r.get("state")) != str(state) or b is None or b <= a:
            continue
        if out and out[-1][1] >= a:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(a, 3), round(b, 3)] for a, b in out]


def intersect(spans: list[list[list[float]]]) -> list[list[float]]:
    """The intersection of several span lists ([[t0, t1], ...] each, sorted, disjoint)."""
    if not spans:
        return []
    cur = spans[0]
    for nxt in spans[1:]:
        out = []
        for a, b in cur:
            for c, d in nxt:
                lo, hi = max(a, c), min(b, d)
                if hi > lo:
                    out.append([lo, hi])
        cur = out
    return cur


def all_top_window(readings: list[dict], slots: list[str] | None = None) -> list[list[float]]:
    """The seconds every slot is on (its on_spans; levi, prereg 135e8160) with its shaper at its top state (levi, prereg
    40843f81); empty when any slot is missing, never on or never at it."""
    by = {r.get("slot"): r for r in readings}
    want = slots or sorted(by)
    if not want or any(t not in by for t in want):
        return []
    per = []
    for t in want:
        r = by[t]
        on = r.get("on_spans")
        if not on:
            return []
        per.append(intersect([r.get("top_state_spans") or [], on]))
    return intersect(per)


def span_s(window: list[list[float]]) -> float:
    return round(float(sum(b - a for a, b in window)), 1)


def session_reading(sd: Path) -> dict:
    """One stress session's readings (read only)."""
    sd = Path(sd)
    meta = json.loads((sd / "meta.json").read_text())
    rows = read_labels(sd / "labels.csv") if (sd / "labels.csv").exists() else []
    t0, t1 = meta.get("started_at"), meta.get("ended_at")
    st = stalls(rows, t1)
    play = playing_seconds(rows)
    by = {c: round(sum(x["s"] for x in st if x["cause"] == c), 3) for c in ("device", "network", "unattributed", "load")}
    tr = tiers(rows)
    platform = meta.get("platform") or sd.parent.name
    rec = {"session": sd.name, "platform": platform, "slot": (meta.get("concurrency") or {}).get("tag"),
           "hold_state": meta.get("hold_state"), "duration_s": round(t1 - t0, 1) if t0 and t1 else None,
           "started_at": t0, "ended_at": t1,
           "channel_url_requested": meta.get("channel_url_requested"), "channel_url": meta.get("channel_url"),
           "geometry": meta.get("geometry_target"), "playing_s": play, "stalls": len(st), "stall_s": by,
           "device_stall_share": round(by["device"] / play, 4) if play else None, "tiers": tr,
           "top_tier": max((int(h) for h, v in tr.items() if v["s"] >= TIER_MIN_S), default=None),
           "media_decoders": meta.get("media_decoders"), "display_start": meta.get("display_start")}
    # the short form's support (restart prereg): playing seconds at the slot's top tier (playing ticks: after startup)
    rec["seconds_at_top_tier"] = tr[str(rec["top_tier"])]["s"] if rec["top_tier"] is not None else None
    # levi on 0097779: a slot counts toward the all-top seconds from its first playing tick (after startup)
    first = next((float(r["ts"]) / 1000.0 for r in rows if _playing(r) and _f(r.get("ts")) is not None), None)
    rec["first_playing_at"] = round(first, 3) if first is not None else None
    rec["on_spans"] = on_spans(rows)
    rec["top_state"] = str(meta.get("hold_state") or TOP_STATE)
    rec["top_state_spans"] = top_state_spans(sd, rec["top_state"], t1)
    play_st = [x for x in st if x["cause"] != "load"]
    rec["playback_stalls"], rec["load_stalls"] = len(play_st), len(st) - len(play_st)
    # levi: no playback stall is readable (share 0); playback stalls none of which carries buffer-ahead are the gap
    rec["device_stall_readable"] = not play_st or any(x["cause"] != "unattributed" for x in play_st)
    if platform == "tiktok":
        hit = [h for h, v in tr.items() if v["s"] >= TIER_MIN_S and (v["rendered_fps"] or 0) >= FPS60_MIN]
        rec["tt_60fps_tier"] = (f"reached (rendered, {hit})" if hit else
                                "tt 60 fps tier not reached (rendered); delivered fps post hoc (LVQoE rendition_fps)")
    return rec


def overlap_s(readings: list[dict]) -> float | None:
    """The seconds every reading's session ran at once (the latest start to the earliest end)."""
    t0 = [r.get("started_at") for r in readings]
    t1 = [r.get("ended_at") for r in readings]
    if not readings or any(not isinstance(x, (int, float)) for x in t0 + t1):
        return None
    return round(max(0.0, min(t1) - max(t0)), 1)


def gate(readings: list[dict], box: dict | None, slots: list[str] | None = None,
         min_session_s: float | None = MIN_SESSION_S) -> dict:
    """The decode item's verdict: per slot device stalls, the box's swap / OOM / CPU headroom (box: box_over's reading,
    CPU over the all-top window, swap / OOM over the whole stress), GPU reported; then the support check (restart prereg, short dry runs;
    levi, prereg 40843f81): >= 5 min with every slot at its top shaping state and every session running, at whatever
    tier the ABR holds, else a pass reads "insufficient". Each slot reports its seconds per tier over those seconds
    (tiers_all_top, filled by with_window_tiers); a slot whose top tier there is below its session's, or that has no
    tier there, is stated. min_session_s None (--short / --minutes): the session-length rule is the support check
    alone."""
    fails, notes, per = [], [], {}
    by_slot = {r.get("slot"): r for r in readings}
    for tag in slots or sorted(by_slot):
        r = by_slot.get(tag)
        if r is None:
            fails.append(f"{tag}: no session")
            per[tag] = {"ok": False, "why": "no session"}
            continue
        if min_session_s is not None and (r["duration_s"] or 0) < min_session_s:
            fails.append(f"{tag}: session {r['duration_s']} s < {min_session_s:.0f} s")
        if not r["device_stall_readable"]:
            per[tag] = {"ok": None, "why": "not readable: no buffer-ahead at any stall (stated gap)"}
            notes.append(f"{tag}: device stalls not readable")
        else:
            ok = (r["device_stall_share"] or 0.0) <= DEVICE_STALL_SHARE_MAX
            per[tag] = {"ok": ok, "device_stall_share": r["device_stall_share"]}
            if not ok:
                fails.append(f"{tag}: device-stall share {r['device_stall_share']} > {DEVICE_STALL_SHARE_MAX}")
        if r.get("tt_60fps_tier", "").startswith("tt 60 fps tier not reached"):
            notes.append(f"{tag}: {r['tt_60fps_tier']}")
    headroom = None
    if box:
        if (box.get("swap_growth_kb") or 0) > 0:
            fails.append(f"swap grew {box['swap_growth_kb']} kB")
        if (box.get("oom_kills") or 0) > 0:
            fails.append(f"{box['oom_kills']} OOM kill(s)")
        p95 = (box.get("cpu_pct") or {}).get("p95")
        headroom = None if p95 is None else p95 > CPU_P95_MAX
        if box.get("gpu_above_report_line"):
            notes.append(f"GPU p95 above 85 % (report): {box['gpu_above_report_line']}")
    else:
        fails.append("no device record")
    want = slots or sorted(by_slot)
    window = all_top_window(readings, want)
    support = M.decode_stress(span_s(window))
    for tag in want:
        r = by_slot.get(tag)
        if r is None or "tiers_all_top" not in r:
            continue
        tw = {h: v["s"] for h, v in r["tiers_all_top"].items()}
        top = max((int(h) for h, v in tw.items() if v >= TIER_MIN_S), default=None)
        per[tag].update({"tiers_all_top_s": tw, "top_tier_all_top": top})
        if top is None:
            notes.append(f"{tag}: no tier held >= {TIER_MIN_S:.0f} s in the all-top seconds")
        elif r.get("top_tier") is not None and top < r["top_tier"]:
            notes.append(f"{tag}: top tier {r['top_tier']} not held >= {TIER_MIN_S:.0f} s in the all-top seconds "
                         f"(there {top}; {tw})")
        elif tw.get(str(top), 0.0) < support["minimum"]["all_top_state_s"]:
            notes.append(f"{tag}: top tier {top} held {tw.get(str(top))} s of the all-top seconds; the rest {tw}")
    st = M.status(not fails, support)
    return {"pass": st == "pass", "status": st, "support": support, "window": window, "fails": fails, "notes": notes,
            "short_of_headroom": headroom, "slots": per}


def with_window_tiers(readings: list[dict], sessions: list, slots: list[str] | None = None) -> list[list[float]]:
    """Fill each reading's tiers_all_top (its seconds per tier over the all-top window) from its labels; returns the
    window."""
    window = all_top_window(readings, slots)
    for r, sd in zip(readings, sessions):
        lab = Path(sd) / "labels.csv"
        r["tiers_all_top"] = tiers(read_labels(lab), window) if lab.exists() else {}
    return window


def box_over(record: Path, window: list[list[float]], t0: float | None, t1: float | None) -> tuple[dict | None, dict]:
    """The device record for the gate (levi on 0097779 / fb78845): CPU p95 (and the GPU, the slots' CPU) over the
    all-top window's spans, their union only (the gaps between spans left out), through gpu_record.summary on the
    record's lines inside them; swap growth and OOM kills, counters (last minus first), over the whole stress [t0, t1],
    so a kill or swap growth at startup or before the last slot starts still fails. With no window, everything over
    the whole stress, stated. box_window records both readings of the counters."""
    sys.path.insert(0, str(NS))
    import gpu_record
    whole = gpu_record.summary(Path(record), t0, t1)
    if not window:
        return whole, {"t0": t0, "t1": t1, "spans": [], "over": "the whole stress (no all-top window)"}
    keep = []
    for line in Path(record).read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("header") or (isinstance(r.get("t"), (int, float)) and _inside(r["t"], window)):
            keep.append(line)
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as fh:
        fh.write("\n".join(keep) + "\n")
        fh.flush()
        box = gpu_record.summary(Path(fh.name))
    w = {"t0": window[0][0], "t1": window[-1][1], "spans": window, "over": "all-top window (union of spans)",
         "counters_over": {"t0": t0, "t1": t1, "over": "the whole stress"},
         "swap_growth_kb": {"whole": whole["swap_growth_kb"], "spans": box["swap_growth_kb"]},
         "oom_kills": {"whole": whole["oom_kills"], "spans": box["oom_kills"]}}
    box["swap_growth_kb"], box["oom_kills"] = whole["swap_growth_kb"], whole["oom_kills"]
    return box, w


def commands(a, comp: dict, tags: list[str], out: Path) -> tuple[list[str], list[str]]:
    """(the device recorder's command, orchestrate's): every slot pinned to a.state and held (--trio-stress), the
    first yt slot fullscreen with --yt-fullscreen (one fullscreen slot), one session per slot, never --trio-record."""
    first_yt = next((t for t in tags if t.startswith("yt")), None)
    pins = []
    for tag in tags:
        spec = f"{tag}:state={a.state}"
        if a.yt_fullscreen and tag == first_yt:
            spec += ",geometry=fullscreen:1920x1080"
        pins += ["--trio-pin", spec]
    box_cmd = [a.python, str(NS / "gpu_record.py"), "--out", str(out / "box_record.jsonl")]
    spec = ",".join(f"{p}={n}" for p, n in comp.items())
    cmd = [a.python, "orchestrate.py", "--batch", spec, "--concurrent", "--slots", spec, "--trio-stress",
           "--max-per-slot", "1", "--trio-layout", a.layout, "--iface", a.iface, *pins, "--trio-pin-reason", "decode_stress"]
    cmd += (["--trio-yt-fullscreen"] if a.yt_fullscreen else []) + (["--dpms-off"] if a.dpms_off else [])
    if getattr(a, "minutes", None):                      # the short form: the held session's base episodes (60 s)
        cmd += ["--episodes", str(int(a.minutes))]
    return box_cmd, cmd


def stop_recorder(box, record: Path, run=subprocess.run, wait_s: float = 15.0) -> dict:
    """Stop the device recorder (levi): a signal to the sudo parent does not reach it (1941f80 / aef581a ran on under
    sudo), so SIGTERM goes to the recorder's own pid (its header line) through sudo -n kill, a join with a timeout, then
    SIGKILL. Returns {pid, term_rc, killed, stop_ok}."""
    pid = None
    try:
        hdr = json.loads(Path(record).read_text().splitlines()[0])
        pid = int(hdr["pid"]) if hdr.get("header") else None
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        pass
    rec: dict = {"pid": pid, "killed": False}
    targets = [str(pid)] if pid else []
    rec["term_rc"] = run(["sudo", "-n", "kill", "-TERM", *targets], capture_output=True).returncode if targets else None
    try:
        box.send_signal(signal.SIGTERM)                # the sudo parent too (harmless when it is gone)
    except (OSError, ProcessLookupError):
        pass
    try:
        box.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        if targets:
            run(["sudo", "-n", "kill", "-KILL", *targets], capture_output=True)
        rec["killed"] = True
        try:
            box.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    rec["stop_ok"] = box.poll() is not None and not rec["killed"]
    return rec


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # a root-owned pid (the recorder under sudo): alive
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


def live_tree(tree_file: Path = TREE_FILE, run=subprocess.run) -> list[str]:
    """What blocks a new launch: the recorded tree's live pids, and any running orchestrate --trio-stress."""
    out = []
    try:
        rec = json.loads(Path(tree_file).read_text())
    except (OSError, ValueError):
        rec = {}
    for k in ("pid", "orchestrate_pid", "box_pid", "recorder_pid"):
        if rec.get(k) and int(rec[k]) != os.getpid() and _alive(rec[k]):
            out.append(f"{k} {rec[k]}")
    try:
        ps = run(["pgrep", "-af", "orchestrate.py"], capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        ps = ""
    out += [f"orchestrate {ln.split()[0]}" for ln in ps.splitlines() if "--trio-stress" in ln]
    return out


def wait_or_stop(proc, stop: dict, clock=time.time, sleep=time.sleep, grace_s: float = STOP_GRACE_S,
                 killpg=os.killpg) -> int | None:
    """Wait for orchestrate; once a stop signal arrived (stop["t"]), give its drain grace_s, then kill its group."""
    while proc.poll() is None:
        if stop.get("t") is not None and clock() - stop["t"] > grace_s and not stop.get("killed"):
            try:
                killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            stop["killed"] = True
        sleep(1.0)
    return proc.returncode


def run(a) -> dict:
    os.environ["LVDC_TRIO_DIR"] = STRESS
    sys.path.insert(0, str(NS))
    import trio_slots as T
    comp = T.parse_composition(a.slots)
    tags = list(T.build_slots(comp))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    box_cmd, cmd = commands(a, comp, tags, out)
    blocking = live_tree()
    if blocking:
        raise SystemExit(f"decode_stress: a stress tree is alive ({', '.join(blocking)}); stop it (or read "
                         f"{TREE_FILE}) before a new launch")
    box = subprocess.Popen((["sudo", "-n"] if os.geteuid() != 0 else []) + box_cmd, stdout=subprocess.DEVNULL,
                           stderr=open(out / "box_record.err", "a"))
    rep: dict = {"what": "t56 decode_stress", "composition": comp, "slots": tags, "state": a.state, "cmd": cmd,
                 "box_cmd": box_cmd, "yt_fullscreen": a.yt_fullscreen, "dpms_off": a.dpms_off}
    t0 = time.time()
    stop: dict = {"t": None, "signal": None}
    orch = None

    def on_signal(sig, _frame):      # forwarded to orchestrate's own drain; the recorder stops in the finally
        stop["t"], stop["signal"] = stop["t"] or time.time(), sig
        if orch is not None and orch.poll() is None:
            try:
                orch.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
    old = {sg: signal.signal(sg, on_signal) for sg in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        TREE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(out / "orchestrate.log", "w") as log:
            orch = subprocess.Popen(cmd, cwd=NS, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            TREE_FILE.write_text(json.dumps({"pid": os.getpid(), "orchestrate_pid": orch.pid, "box_pid": box.pid,
                                             "out": str(out), "t": t0}))
            rep["orchestrate_rc"] = wait_or_stop(orch, stop)
    finally:
        t1 = time.time()
        rep["box_record_stop"] = stop_recorder(box, out / "box_record.jsonl")
        for sg, h in old.items():
            signal.signal(sg, h)
        try:
            TREE_FILE.unlink()
        except OSError:
            pass
        if stop["signal"] is not None:
            rep["stopped"] = {"signal": int(stop["signal"]), "t": stop["t"], "group_killed": bool(stop.get("killed"))}
    rep["t0"], rep["t1"] = t0, t1
    sessions = [d for p in PLATFORMS for d in sorted((DATA / STRESS / p).glob("*"))
                if (d / "meta.json").exists() and (json.loads((d / "meta.json").read_text()).get("started_at") or 0) >= t0 - 5]
    rep["sessions"] = [str(d) for d in sessions]
    read_out(rep, out, sessions, tags, getattr(a, "minutes", None))
    return rep


def read_out(rep: dict, out: Path, sessions: list, tags: list[str], minutes) -> dict:
    """The readings, the all-top window's tiers, the device record over it and the gate, into rep."""
    rep["readings"] = [session_reading(d) for d in sessions]
    window = with_window_tiers(rep["readings"], sessions, tags)
    try:
        rep["box_record"], rep["box_window"] = box_over(Path(out) / "box_record.jsonl", window, rep.get("t0"), rep.get("t1"))
    except Exception as e:
        rep["box_record"] = None
        rep["box_record_error"] = repr(e)[:300]
    rep["gate"] = gate(rep["readings"], rep["box_record"], tags, min_session_s=None if minutes else MIN_SESSION_S)
    return rep


def reread(report: Path, short: bool | None = None) -> dict:
    """A finished stress re-gated with the current reading (its sessions and box_record.jsonl beside report.json);
    the short form unless the report's command had no --episodes (or short says otherwise)."""
    report = Path(report)
    old = json.loads(report.read_text())
    minutes = "--episodes" in (old.get("cmd") or []) if short is None else short
    rep = {k: old.get(k) for k in ("what", "composition", "slots", "state", "cmd", "t0", "t1", "sessions")}
    rep["reread_of"] = {"report": str(report), "gate": old.get("gate")}
    return read_out(rep, report.parent, [Path(d) for d in old.get("sessions") or []], old.get("slots") or [], minutes)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slots", help="the step's composition, e.g. youtube=2,twitch=1,tiktok=1")
    ap.add_argument("--out", help="the stress's report dir (box_record.jsonl, orchestrate.log, report.json)")
    ap.add_argument("--state", default=TOP_STATE, help="the shaper state every slot holds (default the top, 10)")
    ap.add_argument("--yt-fullscreen", action="store_true", help="(a) adopted: the first yt slot fullscreen")
    ap.add_argument("--dpms-off", action="store_true", help="the collection's DPMS setting (levi: fixed for the era)")
    ap.add_argument("--layout", default="composite")
    ap.add_argument("--iface", default="wlp0s20f3")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--read-only", nargs="+", type=Path, help="read these session dirs only (no run, no gate)")
    ap.add_argument("--minutes", type=int, default=None,
                    help="each held session's length in 60 s base episodes (orchestrate --trio-stress --episodes); "
                         "default the collection length (>= 10 min, the full gate). Set: the 10 min session rule gives "
                         "way to the short form's support check (>= 5 min with every slot at its top state, all "
                         "running)")
    ap.add_argument("--short", action="store_true",
                    help=f"the short form (restart prereg): --minutes {M.DECODE_SHORT_MINUTES} unless given")
    ap.add_argument("--reread", type=Path, help="a finished stress's report.json: re-read its sessions and device "
                                                "record with the current gate (writes report_reread.json beside it)")
    a = ap.parse_args(argv)
    if a.reread:
        sys.path.insert(0, str(NS))
        rep = reread(a.reread, True if a.short else None)
        Path(a.reread).with_name("report_reread.json").write_text(json.dumps(rep, indent=1, default=str))
        print(json.dumps(rep["gate"], indent=1, default=str))
        return 0 if rep["gate"]["pass"] else 1
    if a.short and a.minutes is None:
        a.minutes = M.DECODE_SHORT_MINUTES
    if a.read_only:
        print(json.dumps([session_reading(d) for d in a.read_only], indent=1, default=str))
        return 0
    if not a.slots or not a.out:
        ap.error("--slots and --out (or --read-only)")
    rep = run(a)
    Path(a.out, "report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({"gate": rep["gate"], "orchestrate_rc": rep.get("orchestrate_rc")}, indent=1, default=str))
    if rep["gate"]["status"] == "insufficient":
        print(f"decode_stress: insufficient (support {json.dumps(rep['gate']['support']['support'])}, minimum "
              f"{json.dumps(rep['gate']['support']['minimum'])}): not a pass; the full run decides")
    return 0 if rep["gate"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
