#!/usr/bin/env python3
"""t53 gate 2, items 4a and 8 (levi's checklist, LVQoE docs/analysis/design/t53_pilot_gate2_checklist_levi_2026-10-19.md)
from a session's labels.csv and shots/index.jsonl, for shagun's per-session gate-2 json, in both modes (the trio session
and the single-mode reference read the same way). Read-only; no media truth.

  4a visibility: every res_meta tick's document.visibilityState is `visible` (labels.csv `visibility`; a session from
     before the column: "not recorded"). Rendered fps while playing, per tier (labels.csv `total_frames` steps over
     playing intervals, the tier from the ticks' short side, coverage_report.quality_class); the reference-free check
     (levi, until >= 5 single sessions per tier carry total_frames): the shot-free rendered fps >= RENDER_FPS_MIN_SHARE x
     the playing rendition's fps, given per tier (am. 9's rule, frames side: LVQoE scripts/audit/rendition_fps.py;
     --rendition-fps), else "no rendition fps".
  8  dropped frames per playing second outside +- SHOT_GUARD_S of any shot capture (every shots/index.jsonl row's
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
RENDER_FPS_MIN_SHARE = 0.9      # item 4a's reference-free check (levi): rendered fps >= 0.9 x the rendition's fps
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
        out.append({"t0": a["t"], "dt": dt, "dropped": step("dropped"), "total": step("total"),
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
    if not tk or not tk[0]["has_visibility"]:
        vis = {"recorded": False, "ok": None}
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
            f_free = round(sum(x["total"] for x in fr) / s_free, 2) if s_free else None
            ref = (rendition_fps or {}).get(tier)
            fps[tier] = {"fps_shot_free": f_free, "playing_s_shot_free": round(s_free, 1),
                         "fps_all": round(sum(x["total"] for x in xs) / s_all, 2) if s_all else None,
                         "playing_s_all": round(s_all, 1), "rendition_fps": ref, "min_share": RENDER_FPS_MIN_SHARE,
                         "ok": (None if ref is None or f_free is None else f_free >= RENDER_FPS_MIN_SHARE * ref),
                         **({} if ref is not None else {"note": "no rendition fps"})}
    oks = [vis["ok"]] + [v["ok"] for v in fps.values()]
    return {"visibility": vis, "rendered_fps": fps if tk and tk[0]["has_total"] else {"recorded": False},
            "ok": None if any(o is None for o in oks) else all(oks)}


def session_items(sd: Path, rendition_fps: dict | None = None) -> dict:
    sd = Path(sd)
    tk, spans = ticks(sd), shot_spans(sd)
    iv = intervals(tk, spans)
    meta = {}
    try:
        meta = json.loads((sd / "meta.json").read_text())
    except (OSError, ValueError):
        pass
    return {"session": str(sd), "mode": (meta.get("concurrency") or {}).get("mode", "single"),
            "ticks": len(tk), "item4a": item4a(tk, iv, rendition_fps), "item8": item8(iv, len(spans))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--rendition-fps", type=Path, default=None,
                    help="json {session name: {tier: fps}} (LVQoE scripts/audit/rendition_fps.py)")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    fps = json.loads(a.rendition_fps.read_text()) if a.rendition_fps else {}
    rows = []
    for sd in a.sessions:
        try:
            rows.append(session_items(sd, (fps.get(sd.name) or {}).get("tiers") or fps.get(sd.name)))
        except Exception as e:  # noqa: BLE001 -- one session's failure is reported
            rows.append({"session": str(sd), "error": repr(e)[:300]})
        print(json.dumps(rows[-1]), flush=True)
    if a.out:
        a.out.write_text(json.dumps({"sessions": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
