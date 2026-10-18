#!/usr/bin/env python3
"""coverage_report.py: class coverage of the accepted sessions, and the coverage-steered shaper matrix (collection v2
prereg Addendum Q, 2026-10-18; rushi via supervisor: every tier, 720p and 1080p included, and stalls; steer by labels).

Per session (labels.csv res_meta ticks, 1 Hz): a tick is STALL while the stall label is on (a stall event, until the
next start / video_start); an ad tick is left out; else its class is the frozen pipeline's rule on the short side (LVQoE
watcher.lv.constants.quality_to_video_state(min(w, h)): <= 200 Q_144p, <= 300 Q_240p, <= 420 Q_360p, <= 600 Q_480p,
<= 900 Q_720p, else Q_1080p; 0 = no class). Minutes per class = ticks / 60: a steering measure on the raw ticks, not
the frozen labels. Covariates: buffered_ahead_s p10 / median / p90 over the playing ticks; twitch: the latency mode and
the latency to broadcaster from player_stats.jsonl's Video Stats records.
Accepted = check.json PASS and session_class normal (none recorded: PASS), outside _failed (Addendum Q: no outcome-based
exclusion; the class mix never decides inclusion).

Snapshot per platform (Addendum Q): per class of the platform alphabet, the accepted sessions with >= FLOOR_MINUTES of
it (tt tier classes count HEVC-ladder sessions only, meta tt_stream.codec; amendment 2); the pooled stall share; tt:
room entries per entry state over every checked session, accepted or _failed, except not_live / login_wall / interrupted
(amendment 3: startup_failure / player_dead sessions are the startup reading's events); a stream entry counts at the
first shaped state after it, a re-entry at the state in force; per matrix: accepted sessions, mean minutes per class,
pooled stall share.

The matrix rule (RULE_VERSION, deterministic):
  1. target = the largest deficit to its floor: a class, FLOOR_SESSIONS - its sessions; the stall band, the shortfall as
     a share of the band edge times FLOOR_SESSIONS; a tt entry state, TT_ENTRY_FLOOR - its entries. Ties: class order,
     then the stall band, then states.
  2. matrix = of the allowed matrices, one with fewer than EXPLORE_MIN accepted sessions first (class / stall targets:
     every matrix gets a yield before the yields decide); else the highest yield for the target: a class, its mean
     minutes per session; stall too low / too high, the matrix's pooled stall share, highest / lowest; an entry state,
     the matrix's stationary probability of it. Ties by matrix id (the first in sort order).
  3. no deficit: the platform's default matrix.
orchestrate writes the record (snapshot, deficits, yields, choice, rule) to data/_summaries/coverage_last.json and
LVDC_COVERAGE_JSON; exp.py puts it in meta.json coverage, marked applied iff its platform and matrix are the session's.

Usage: python coverage_report.py [--data-dir ../data] [--date YYYY-MM-DD]   (writes _summaries/coverage_<date>.json/.md)
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
from pathlib import Path

import numpy as np

from utils.shaping import PLATFORM_MATRICES, stationary_distribution

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RULE_VERSION = "coverage-v1 (2026-10-18)"
FLOOR_SESSIONS, FLOOR_MINUTES = 5, 2.0          # Addendum Q: N = 5 sessions with >= M = 2 minutes of every class
TT_ENTRY_FLOOR = 5                              # Addendum Q: every tt shaper state at >= 5 room entries
STALL_BAND = {"youtube": (0.046, 0.183), "twitch": (0.029, 0.115), "tiktok": (0.037, 0.147)}
CLASSES = {"youtube": ["STALL", "Q_144p", "Q_240p", "Q_360p", "Q_480p", "Q_720p", "Q_1080p"],
           "twitch": ["STALL", "Q_144p", "Q_360p", "Q_480p", "Q_720p", "Q_1080p"],
           "tiktok": ["STALL", "Q_360p", "Q_480p", "Q_720p", "Q_1080p"]}
DEFAULT_MATRIX = {"youtube": "A", "tiktok": "TT-M", "twitch": "TW-M"}
ENTRY_EXCLUDED_CLASSES = ("not_live", "login_wall", "interrupted")   # amendment 3: the entry did not happen as designed
EXPLORE_MIN = 2
FLAG_AFTER_SESSIONS = 5                         # supervisor 2026-10-18: a class with zero support after 5 sessions: FLAG
SUMMARY_FILE = "_summaries/coverage_last.json"


def quality_class(w: float, h: float) -> str | None:
    q = min(w, h)
    if q <= 0:
        return None
    for lim, c in ((200, "Q_144p"), (300, "Q_240p"), (420, "Q_360p"), (600, "Q_480p"), (900, "Q_720p")):
        if q <= lim:
            return c
    return "Q_1080p"


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def _q(a: list[float], ps=(10, 50, 90)) -> dict | None:
    if not a:
        return None
    return {("median" if p == 50 else f"p{p}"): round(float(x), 2) for p, x in zip(ps, np.percentile(a, ps))}


def _load(p: Path) -> dict:
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}


def session_classes(sd: Path) -> dict:
    """{minutes per class, stall_share, buffer_s quantiles over the playing ticks} from labels.csv."""
    p = Path(sd) / "labels.csv"
    if not p.exists():
        return {"minutes": {}, "stall_share": None, "buffer_s": None}
    ticks: dict = collections.Counter()
    buf, stall = [], False
    with open(p, newline="") as fh:
        for r in csv.DictReader(fh):
            ty = r.get("type")
            if ty == "stall":
                stall = True
            elif ty in ("start", "video_start"):
                stall = False
            if ty != "res_meta" or str(r.get("is_ad")).lower() in ("true", "1"):
                continue
            if stall:
                ticks["STALL"] += 1
                continue
            c = quality_class(_f(r.get("video_w")) or 0, _f(r.get("video_h")) or 0)
            if c:
                ticks[c] += 1
                b = _f(r.get("buffered_ahead_s"))
                if b is not None:
                    buf.append(b)
    n = sum(ticks.values())
    return {"minutes": {k: round(v / 60, 3) for k, v in sorted(ticks.items())},
            "stall_share": round(ticks["STALL"] / n, 4) if n else None, "buffer_s": _q(buf)}


def twitch_latency(sd: Path) -> dict | None:
    """{modes: count per Latency Mode, latency_s: median / p90 of Latency To Broadcaster} from player_stats.jsonl."""
    p = Path(sd) / "player_stats.jsonl"
    if not p.exists():
        return None
    modes, lat = collections.Counter(), []
    with open(p) as fh:
        for line in fh:
            try:
                v = (json.loads(line).get("stats") or {}).get("twitch_video_stats") or {}
            except ValueError:
                continue
            if v.get("Latency Mode"):
                modes[v["Latency Mode"]] += 1
            x = _f(str(v.get("Latency To Broadcaster") or "").split(" ")[0])
            if x is not None:
                lat.append(x)
    if not modes and not lat:
        return None
    return {"modes": dict(modes), "latency_s": _q(lat, (50, 90)) if lat else None}


def accepted_sessions(data_dir: Path, platform: str) -> list[Path]:
    out = []
    for chk in (Path(data_dir) / platform).glob("*/check.json"):
        c = _load(chk)
        if c.get("verdict") == "PASS" and (c.get("session_class") or "normal") == "normal":
            out.append(chk.parent)
    return sorted(out)


def entry_states(meta: dict) -> list[str]:
    out = []
    for e in meta.get("entries") or []:
        st = ((e.get("first_shaped_after") or {}).get("state") if e.get("kind") == "stream"
              else (e.get("shaper_at_entry") or {}).get("state"))
        if st not in (None, "unshaped"):
            out.append(str(st))
    return out


def entry_sessions(data_dir: Path, platform: str) -> list[tuple[Path, dict]]:
    """(session dir, check.json) of every checked session, accepted or _failed, whose class is not excluded."""
    out = []
    for c in sorted(list((Path(data_dir) / platform).glob("*/check.json"))
                    + list((Path(data_dir) / "_failed" / platform).glob("*/check.json"))):
        chk = _load(c)
        if chk and chk.get("session_class") not in ENTRY_EXCLUDED_CLASSES:
            out.append((c.parent, chk))
    return out


def snapshot(data_dir: Path, platform: str) -> dict:
    classes = CLASSES[platform]
    cnt, mins = dict.fromkeys(classes, 0), dict.fromkeys(classes, 0.0)
    st_min = lab_min = 0.0
    entries, codecs = collections.Counter(), collections.Counter()
    pm = collections.defaultdict(lambda: {"n": 0, "n_tier": 0, "min": collections.Counter(), "stall": 0.0, "lab": 0.0})
    sessions = accepted_sessions(data_dir, platform)
    for sd in sessions:
        meta = _load(sd / "meta.json")
        sc = session_classes(sd)
        tier_ok = True
        if platform == "tiktok":
            codec = (meta.get("tt_stream") or {}).get("codec") or "unknown"
            codecs[codec] += 1
            tier_ok = codec == "hevc"                    # amendment 2: tt tier floors count HEVC-ladder sessions only
        m = pm[(meta.get("shaper") or {}).get("matrix_id") or "unknown"]
        tot, st = sum(sc["minutes"].values()), sc["minutes"].get("STALL", 0.0)
        st_min, lab_min = st_min + st, lab_min + tot
        m["n"] += 1
        m["n_tier"] += tier_ok
        m["stall"] += st
        m["lab"] += tot
        for c in classes:
            if c != "STALL" and not tier_ok:
                continue
            v = sc["minutes"].get(c, 0.0)
            mins[c] += v
            m["min"][c] += v
            cnt[c] += v >= FLOOR_MINUTES
    out = {"platform": platform, "rule": RULE_VERSION, "n_sessions": len(sessions),
           "classes": {c: {"sessions_ge_min": cnt[c], "minutes": round(mins[c], 2)} for c in classes},
           "stall_share_pooled": round(st_min / lab_min, 4) if lab_min else None, "stall_band": list(STALL_BAND[platform]),
           "per_matrix": {k: {"sessions": v["n"], "tier_sessions": v["n_tier"],
                              "class_minutes_mean": {c: round(v["min"][c] / (v["n"] if c == "STALL" else v["n_tier"]), 3)
                                                     if (v["n"] if c == "STALL" else v["n_tier"]) else None for c in classes},
                              "stall_share": round(v["stall"] / v["lab"], 4) if v["lab"] else None}
                          for k, v in sorted(pm.items())}}
    if platform == "tiktok":
        es = entry_sessions(data_dir, platform)
        for sd, chk in es:
            entries.update(entry_states({"entries": (_load(sd / "meta.json").get("entries") or chk.get("entries") or [])}))
        out.update(tt_entries_by_state=dict(sorted(entries.items())), tt_entry_sessions=len(es),
                   tt_codec_sessions=dict(sorted(codecs.items())))
    return out


def deficits(snap: dict, states: list[str] = ()) -> list[dict]:
    """The floors' shortfalls, largest first (ties: class order, the stall band, states)."""
    out = []
    for i, (c, v) in enumerate(snap["classes"].items()):
        if v["sessions_ge_min"] < FLOOR_SESSIONS:
            out.append({"kind": "class", "key": c, "deficit": float(FLOOR_SESSIONS - v["sessions_ge_min"]), "order": i})
    lo, hi = snap["stall_band"]
    s = snap.get("stall_share_pooled")
    if s is not None and s < lo:
        out.append({"kind": "stall_low", "key": "stall_share", "deficit": round(FLOOR_SESSIONS * (lo - s) / lo, 4), "order": 50})
    elif s is not None and s > hi:
        out.append({"kind": "stall_high", "key": "stall_share", "deficit": round(FLOOR_SESSIONS * (s - hi) / hi, 4), "order": 50})
    if "tt_entries_by_state" in snap:
        for st in sorted(states, key=int):
            n = snap["tt_entries_by_state"].get(st, 0)
            if n < TT_ENTRY_FLOOR:
                out.append({"kind": "entry_state", "key": st, "deficit": float(TT_ENTRY_FLOOR - n), "order": 100 + int(st)})
    return sorted(out, key=lambda d: (-d["deficit"], d["order"]))


def pick_matrix(snap: dict, matrices: dict, allowed: list[str] | None = None) -> dict:
    """The rule (module docstring) on a snapshot. matrices: the platform's {id: (transitions, low cap, init rule)}."""
    platform = snap["platform"]
    ids = sorted(allowed or matrices)
    states = sorted({s for m in ids for s in matrices[m][0]}, key=int) if platform == "tiktok" else []
    defs = deficits(snap, states)
    rec = {"platform": platform, "rule": RULE_VERSION, "allowed": ids, "deficits": defs, "snapshot": snap}
    if not defs:
        return {**rec, "matrix": DEFAULT_MATRIX[platform], "target": None, "yields": {}, "why": "no deficit: default"}
    tgt, pm = defs[0], snap["per_matrix"]
    if tgt["kind"] == "entry_state":
        yields = {m: round(stationary_distribution(matrices[m][0]).get(tgt["key"], 0.0), 6) for m in ids}
        why = "stationary probability of the state"
    else:
        n_key = "sessions" if tgt["kind"] != "class" or tgt["key"] == "STALL" else "tier_sessions"
        young = [m for m in ids if (pm.get(m) or {}).get(n_key, 0) < EXPLORE_MIN]
        if young:
            return {**rec, "matrix": young[0], "target": tgt, "yields": {},
                    "why": f"explore: fewer than {EXPLORE_MIN} accepted sessions on {young}"}
        if tgt["kind"] == "class":
            yields = {m: pm[m]["class_minutes_mean"][tgt["key"]] or 0.0 for m in ids}
            why = "mean minutes of the class per session"
        else:
            sign = 1 if tgt["kind"] == "stall_low" else -1
            yields = {m: sign * (pm[m]["stall_share"] or 0.0) for m in ids}
            why = f"pooled stall share ({'highest' if sign > 0 else 'lowest'})"
    top = max(yields.values())
    return {**rec, "matrix": min(m for m in ids if yields[m] == top), "target": tgt, "yields": yields, "why": why}


def choose(data_dir: Path, platform: str, allowed: list[str] | None = None) -> dict:
    """orchestrate, before each session: the snapshot and the rule's choice for `platform`."""
    rec = pick_matrix(snapshot(data_dir, platform), PLATFORM_MATRICES[platform], allowed)
    return {**rec, "taken_at": dt.datetime.now().isoformat(timespec="seconds")}


def meta_record(rec: dict | None, platform: str, matrix_id: str | None) -> dict | None:
    """exp.py: the coverage record for meta.json, marked applied iff it is for this platform and this matrix."""
    if not rec:
        return None
    return {**rec, "applied": rec.get("platform") == platform and rec.get("matrix") == matrix_id}


def coverage_flags(snap: dict) -> list[str]:
    """FLAG lines: a class with no session >= FLOOR_MINUTES once the platform has FLAG_AFTER_SESSIONS accepted sessions."""
    p, n = snap["platform"], snap["n_sessions"]
    if n < FLAG_AFTER_SESSIONS:
        return []
    hevc = (snap.get("tt_codec_sessions") or {}).get("hevc", 0)
    return [f"FLAG {p}: {c} has zero support after {n} accepted sessions (none with >= {FLOOR_MINUTES} min; "
            f"{v['minutes']} min in all)" + (f"; tt tiers count HEVC sessions only: {hevc} of {n}" if p == "tiktok" and c != "STALL" else "")
            for c, v in snap["classes"].items() if v["sessions_ge_min"] == 0]


def regime_flags(data_dir: Path, platform: str) -> list[str]:
    """FLAG lines from check.json suspicious (stream_regime.py), accepted and _failed sessions: per flag, the sessions
    raising it, the first and the latest (check.json write order)."""
    rows = []
    for c in list((Path(data_dir) / platform).glob("*/check.json")) + list((Path(data_dir) / "_failed" / platform).glob("*/check.json")):
        chk = _load(c)
        if chk:
            rows.append((c.stat().st_mtime, c.parent.name, [x.get("flag") for x in chk.get("suspicious") or []]))
    rows.sort()
    by = collections.defaultdict(list)
    for _, name, flags in rows:
        for f in flags:
            by[f].append(name)
    return [f"FLAG {platform} regime: {f} on {len(v)} of {len(rows)} checked sessions (first {v[0]}, latest {v[-1]})"
            for f, v in sorted(by.items())]


def report(data_dir: Path, platforms=("youtube", "twitch", "tiktok")) -> dict:
    """The on-demand / daily coverage report: per platform the snapshot, the deficits and per accepted session its class
    minutes, stall share, buffer quantiles, (twitch) latency, (tt) codec."""
    out = {"rule": RULE_VERSION, "floors": {"sessions": FLOOR_SESSIONS, "minutes": FLOOR_MINUTES,
                                            "stall_band": STALL_BAND, "tt_entries_per_state": TT_ENTRY_FLOOR},
           "platforms": {}}
    for p in platforms:
        snap = snapshot(data_dir, p)
        rows = []
        for sd in accepted_sessions(data_dir, p):
            meta = _load(sd / "meta.json")
            row = {"session": sd.name, "matrix": (meta.get("shaper") or {}).get("matrix_id"), **session_classes(sd)}
            if p == "twitch":
                row["twitch_latency"] = twitch_latency(sd)
            if p == "tiktok":
                row["tt_codec"] = (meta.get("tt_stream") or {}).get("codec")
            rows.append(row)
        med = [r["buffer_s"]["median"] for r in rows if r["buffer_s"]]
        lat = [r["twitch_latency"]["latency_s"]["median"] for r in rows if (r.get("twitch_latency") or {}).get("latency_s")]
        out["platforms"][p] = {"snapshot": snap,
                               "deficits": deficits(snap, sorted(snap.get("tt_entries_by_state", {}), key=int)),
                               "flags": coverage_flags(snap) + regime_flags(data_dir, p),
                               "buffer_median_s_over_sessions": _q(med), "twitch_latency_median_s_over_sessions": _q(lat),
                               "sessions": rows}
    return out


def markdown(rep: dict) -> list[str]:
    f = rep["floors"]
    md = [f"## Coverage (Addendum Q, {rep['rule']}): accepted sessions with >= {f['minutes']} min of each class "
          f"(floor {f['sessions']}); pooled stall share vs band", ""]
    flags = [x for v in rep["platforms"].values() for x in v.get("flags", [])]
    md += [f"- {x}" for x in flags] + ([""] if flags else [])
    for p, v in rep["platforms"].items():
        s = v["snapshot"]
        md += [f"**{p}**: {s['n_sessions']} accepted; stall share {s['stall_share_pooled']} (band {s['stall_band']}); "
               f"buffer median over sessions {v['buffer_median_s_over_sessions']}"
               + (f"; latency to broadcaster {v['twitch_latency_median_s_over_sessions']}" if p == "twitch" else "")
               + (f"; codec sessions {s['tt_codec_sessions']} (tiers: hevc only); entries by state "
                  f"{s['tt_entries_by_state']} (over {s['tt_entry_sessions']} sessions, any class but not_live / login_wall / "
                  f"interrupted)" if p == "tiktok" else ""), "",
               "| class | sessions >= floor min | minutes |", "|---|---|---|"]
        md += [f"| {c} | {x['sessions_ge_min']} | {x['minutes']} |" for c, x in s["classes"].items()]
        md += ["", "deficits: " + (", ".join(f"{d['key']} {d['deficit']}" for d in v["deficits"]) or "none"), ""]
    return md


def write(data_dir: Path = DATA_DIR, day: str | None = None) -> Path:
    day = day or dt.date.today().isoformat()
    rep = report(data_dir)
    dest = Path(data_dir) / "_summaries"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"coverage_{day}.json").write_text(json.dumps(rep, indent=1, default=str))
    (dest / f"coverage_{day}.md").write_text("\n".join(markdown(rep)))
    return dest / f"coverage_{day}.md"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    out = write(a.data_dir, a.date)
    print(out.read_text())
    print(f"→ {out}")
