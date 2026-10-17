"""motion.py: the display-side motion cross-check from the 10 s screenshots (LVQoE prereg addendum O and its
amendment, 2026-10-17). Ground truth only, quarantined like player_stats: motion.jsonl sits beside player_stats.jsonl
and is never a feature or a model input. The primary complexity measure is the frame sizes parsed from the decrypted
media (LVQoE t50); this is its cross-check (R-O3).

Per consecutive pair of cadence shots (shots/index.jsonl, reason "cadence"):
  crop      both shots to the player rectangle recorded with the shot (CSS rect x shot width / viewport width), so
            chat and side panes stay outside; grey; downscaled to 64 x 36
  motion    mean absolute difference / 255 (downscaled MAD)
  valid     both shots' ticks are play: stall label off, outside the ad spans (check_session's sensor_v2 span rule) and
            not an ad tick, and the playhead advanced between the two ticks; frozen frames never read as static
  scene_cut motion > SCENE_CUT (reported)
Per 30 s window (by the later shot): the median over its valid pairs; session motion: the median over windows; n/a
with fewer than MIN_VALID_PAIRS valid pairs, or when the shots carry no player rectangle (sessions before 2026-10-17).

    python motion.py <session_dir>     (writes <session_dir>/motion.jsonl and prints the summary)
"""
from __future__ import annotations

import csv
import io
import json
import statistics
import sys
from pathlib import Path

GRID = (64, 36)
SCENE_CUT = 0.2
NEAR_ZERO = 0.002
WINDOW_S = 30.0
MIN_VALID_PAIRS = 30


def _tick_states(sd: Path) -> dict:
    """event_id -> (ts s, stall label, ad, current_time) for every res_meta tick."""
    from check_session import ad_spans, read_labels, _f, _true
    rows = read_labels(sd)
    meta = [r for r in rows if r.get("type") == "res_meta"]
    spans, _ = ad_spans(meta) if meta and "ad_signals" in meta[0] else ([], 0)
    out, stall = {}, False
    for r in rows:
        ty = r.get("type")
        if ty == "stall":
            stall = True
        elif ty in ("start", "video_start"):
            stall = False
        if ty != "res_meta":
            continue
        t = (_f(r.get("ts")) or 0) / 1000.0
        ad = _true(r.get("is_ad")) or any(lo <= t <= hi for lo, hi in spans)
        try:
            out[int(float(r.get("event_id")))] = (t, stall, ad, _f(r.get("current_time")))
        except (TypeError, ValueError):
            pass
    return out


def _tick_id(v) -> int | None:
    """shots/index.jsonl stores the tick as the page's dataset string ('117'); labels.csv event ids are numbers."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _grey(path: Path, shot: dict):
    from PIL import Image
    im = Image.open(io.BytesIO(path.read_bytes())).convert("L")
    if shot.get("crop") == "player":                  # the shot is the player rectangle already (x11 grab)
        return list(im.resize(GRID, Image.BILINEAR).getdata())
    p = shot.get("player") or {}
    rect, inner = p.get("rect"), p.get("inner")
    if not rect or not inner or not inner[0]:
        return None
    k = im.width / float(inner[0])
    x0, y0 = max(0, round(rect[0] * k)), max(0, round(rect[1] * k))
    x1, y1 = min(im.width, round((rect[0] + rect[2]) * k)), min(im.height, round((rect[1] + rect[3]) * k))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return list(im.crop((x0, y0, x1, y1)).resize(GRID, Image.BILINEAR).getdata())


def compute(sd: Path, write: bool = True) -> dict:
    sd = Path(sd)
    idx = sd / "shots" / "index.jsonl"
    if not idx.exists():
        return {"status": "n/a", "why": "no shots"}
    shots = [json.loads(ln) for ln in idx.read_text().splitlines() if ln.strip()]
    shots = sorted((s for s in shots if s.get("reason") == "cadence"), key=lambda s: s["ts"])
    ticks = _tick_states(sd)
    pairs, prev, prev_g = [], None, None
    for s in shots:
        g = _grey(sd / "shots" / s["file"], s)
        if prev is not None:
            rec = {"t0": prev["ts"], "t1": s["ts"], "file0": prev["file"], "file1": s["file"], "mad": None,
                   "scene_cut": None, "valid": False, "why": None}
            if g is None or prev_g is None:
                rec["why"] = "no player rect"
            else:
                rec["mad"] = sum(abs(a - b) for a, b in zip(g, prev_g)) / (len(g) * 255.0)
                rec["scene_cut"] = rec["mad"] > SCENE_CUT
                a, b = ticks.get(_tick_id(prev.get("tick"))), ticks.get(_tick_id(s.get("tick")))
                if a is None or b is None:
                    rec["why"] = "no tick"
                elif a[1] or b[1]:
                    rec["why"] = "stall"
                elif a[2] or b[2]:
                    rec["why"] = "ad"
                elif a[3] is None or b[3] is None or not b[3] > a[3]:
                    rec["why"] = "playhead not advancing"
                else:
                    rec["valid"] = True
            pairs.append(rec)
        prev, prev_g = s, g
    if write:
        with open(sd / "motion.jsonl", "w") as fh:
            for r in pairs:
                fh.write(json.dumps(r) + "\n")
    return summary(pairs)


def summary(pairs: list[dict]) -> dict:
    valid = [p for p in pairs if p["valid"]]
    out = {"pairs": len(pairs), "valid_pairs": len(valid),
           "invalid_by_reason": {k: sum(1 for p in pairs if p["why"] == k) for k in {p["why"] for p in pairs if p["why"]}}}
    if len(valid) < MIN_VALID_PAIRS:
        return {**out, "status": "n/a", "why": f"fewer than {MIN_VALID_PAIRS} valid pairs"}
    t0 = min(p["t1"] for p in valid)
    win: dict[int, list] = {}
    for p in valid:
        win.setdefault(int((p["t1"] - t0) // WINDOW_S), []).append(p["mad"])
    m = sorted(p["mad"] for p in valid)
    return {**out, "status": "ok", "session_motion": statistics.median(statistics.median(v) for v in win.values()),
            "median": statistics.median(m), "p90": m[min(len(m) - 1, int(0.9 * len(m)))],
            "share_near_zero": sum(1 for x in m if x < NEAR_ZERO) / len(m),
            "scene_cuts": sum(1 for p in valid if p["scene_cut"]), "windows": len(win)}


def read_summary(sd: Path) -> dict | None:
    p = Path(sd) / "motion.jsonl"
    if not p.exists():
        return None
    return summary([json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()])


if __name__ == "__main__":
    print(json.dumps(compute(Path(sys.argv[1]))))
