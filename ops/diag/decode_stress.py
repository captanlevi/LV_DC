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
The null comes first and is not this script's: the same device-stall definition on the frozen single-mode reference
sessions (p90 above 2 % stops the gate), the readers' (brad / levi); --read-only reads any session dirs with it.

    python3 ops/diag/decode_stress.py --slots youtube=2,twitch=1,tiktok=1 --out <dir> [--yt-fullscreen] [--dpms-off]
    python3 ops/diag/decode_stress.py --read-only <session_dir> ...        (the readings alone, no run)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

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


def playing_seconds(rows: list[dict]) -> float:
    """Playing res_meta ticks, each weighted by its gap to the next tick (capped at 2 s)."""
    ticks = [r for r in rows if r.get("type") == "res_meta" and _f(r.get("ts")) is not None]
    s = 0.0
    for a, b in zip(ticks, ticks[1:]):
        if _playing(a):
            s += min(max(float(b["ts"]) - float(a["ts"]), 0.0) / 1000.0, 2.0)
    return round(s, 3)


def tiers(rows: list[dict]) -> dict:
    """Per video height: playing support seconds and rendered fps (total_frames over consecutive playing ticks of that
    height), and the dropped share over the same pairs."""
    ticks = [r for r in rows if r.get("type") == "res_meta" and _f(r.get("ts")) is not None]
    agg: dict = {}
    for a, b in zip(ticks, ticks[1:]):
        if not (_playing(a) and _playing(b)) or a.get("video_h") != b.get("video_h"):
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
           "channel_url_requested": meta.get("channel_url_requested"), "channel_url": meta.get("channel_url"),
           "geometry": meta.get("geometry_target"), "playing_s": play, "stalls": len(st), "stall_s": by,
           "device_stall_share": round(by["device"] / play, 4) if play else None, "tiers": tr,
           "top_tier": max((int(h) for h, v in tr.items() if v["s"] >= TIER_MIN_S), default=None),
           "media_decoders": meta.get("media_decoders"), "display_start": meta.get("display_start")}
    play_st = [x for x in st if x["cause"] != "load"]
    rec["playback_stalls"], rec["load_stalls"] = len(play_st), len(st) - len(play_st)
    # levi: no playback stall is readable (share 0); playback stalls none of which carries buffer-ahead are the gap
    rec["device_stall_readable"] = not play_st or any(x["cause"] != "unattributed" for x in play_st)
    if platform == "tiktok":
        hit = [h for h, v in tr.items() if v["s"] >= TIER_MIN_S and (v["rendered_fps"] or 0) >= FPS60_MIN]
        rec["tt_60fps_tier"] = (f"reached (rendered, {hit})" if hit else
                                "tt 60 fps tier not reached (rendered); delivered fps post hoc (LVQoE rendition_fps)")
    return rec


def gate(readings: list[dict], box: dict | None, slots: list[str] | None = None) -> dict:
    """The decode item's verdict: per slot device stalls, the box's swap / OOM / CPU headroom, GPU reported."""
    fails, notes, per = [], [], {}
    by_slot = {r.get("slot"): r for r in readings}
    for tag in slots or sorted(by_slot):
        r = by_slot.get(tag)
        if r is None:
            fails.append(f"{tag}: no session")
            per[tag] = {"ok": False, "why": "no session"}
            continue
        if (r["duration_s"] or 0) < MIN_SESSION_S:
            fails.append(f"{tag}: session {r['duration_s']} s < {MIN_SESSION_S:.0f} s")
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
    return {"pass": not fails, "fails": fails, "notes": notes, "short_of_headroom": headroom, "slots": per}


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


def run(a) -> dict:
    os.environ["LVDC_TRIO_DIR"] = STRESS
    sys.path.insert(0, str(NS))
    import trio_slots as T
    import gpu_record
    comp = T.parse_composition(a.slots)
    tags = list(T.build_slots(comp))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    box_cmd, cmd = commands(a, comp, tags, out)
    box = subprocess.Popen((["sudo", "-n"] if os.geteuid() != 0 else []) + box_cmd, stdout=subprocess.DEVNULL,
                           stderr=open(out / "box_record.err", "a"))
    rep: dict = {"what": "t56 decode_stress", "composition": comp, "slots": tags, "state": a.state, "cmd": cmd,
                 "box_cmd": box_cmd, "yt_fullscreen": a.yt_fullscreen, "dpms_off": a.dpms_off}
    t0 = time.time()
    try:
        with open(out / "orchestrate.log", "w") as log:
            rep["orchestrate_rc"] = subprocess.call(cmd, cwd=NS, stdout=log, stderr=subprocess.STDOUT)
    finally:
        t1 = time.time()
        rep["box_record_stop"] = stop_recorder(box, out / "box_record.jsonl")
    rep["t0"], rep["t1"] = t0, t1
    sessions = [d for p in PLATFORMS for d in sorted((DATA / STRESS / p).glob("*"))
                if (d / "meta.json").exists() and (json.loads((d / "meta.json").read_text()).get("started_at") or 0) >= t0 - 5]
    rep["sessions"] = [str(d) for d in sessions]
    rep["readings"] = [session_reading(d) for d in sessions]
    try:
        rep["box_record"] = gpu_record.summary(out / "box_record.jsonl", t0, t1)
    except Exception as e:
        rep["box_record"] = None
        rep["box_record_error"] = repr(e)[:300]
    rep["gate"] = gate(rep["readings"], rep["box_record"], tags)
    return rep


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
    a = ap.parse_args(argv)
    if a.read_only:
        print(json.dumps([session_reading(d) for d in a.read_only], indent=1, default=str))
        return 0
    if not a.slots or not a.out:
        ap.error("--slots and --out (or --read-only)")
    rep = run(a)
    Path(a.out, "report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({"gate": rep["gate"], "orchestrate_rc": rep.get("orchestrate_rc")}, indent=1, default=str))
    return 0 if rep["gate"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
