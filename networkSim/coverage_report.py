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

Snapshot per platform (Addendum Q): per tier class of the platform alphabet, the accepted sessions with >= FLOOR_MINUTES
of it (tt tier classes count HEVC-ladder sessions only, meta tt_stream.codec; amendment 2); STALL (amendment 5): the
sessions with >= STALL_FLOOR_ONSETS stall onsets (an event = a run of consecutive STALL ticks), the pooled onset count
and the run-duration distribution; stall minutes and the pooled share are covariates (the share band is reported, no
longer a target); tt:
room entries per entry state over every checked session, accepted or _failed, except not_live / login_wall / interrupted
(amendment 3: startup_failure / player_dead sessions are the startup reading's events); a stream entry counts at the
first shaped state after it, a re-entry at the state in force; per matrix: accepted sessions, mean minutes per class,
mean stall onsets, pooled stall share.

The matrix rule (RULE_VERSION, deterministic):
  1. target = the largest deficit share (missing / floor; Q am. 17): a class (STALL included), FLOOR_SESSIONS - its
     sessions; a tt entry state, TT_ENTRY_FLOOR - its entries; a schedule family (am. 17), FAMILY_FLOOR - its accepted
     sessions, met by drawing the family itself. Ties: class order, then states, then families.
  2. matrix = of the allowed matrices, one with fewer than EXPLORE_MIN accepted sessions first (class targets: every
     matrix gets a yield before the yields decide; among several, EXPLORE_RULE: the fewest record attempts, ties by a
     seeded draw, a matrix failing EXPLORE_REPEAT_CAP times in a row passed over; coverage.explore records the pick);
     else the highest yield for the target: a tier class, its mean
     minutes per session; STALL, its mean onsets per session; an entry state, the matrix's stationary probability of
     it. Ties by matrix id (the first in sort order).
  3. no deficit: rotation over the allowed matrices, the least-collected first, ties to the lowest id
     (coverage.default_rotation; Q am. 15 (2)).
Q am. 15 (3) (revised 9dfbb62): a tier-class target is drawn only among matrices whose stationary mass in the class's
state band is >= FLOOR_MINUTES / the session's episodes (yt 2/21, twitch / tt 2/23); the band = the shaper states whose
rate (STATE_RATE: the midpoint of the state's range) is >= the margin x the tier's rung (RUNGS: kbit/s, kind, source;
a declared-peak rung, twitch's BANDWIDTH, x 1.1; a declared-average rung, yt's format bitrate / tt's vbitrate, x 1.4).
A class no allowed matrix qualifies for is skipped (the next deficit is the target) and listed in
coverage.unreachable_targets. yt Q_1080p couples to the fullscreen geometry (coverage.geometry_forced). The band map
(band, rung, margin, masses, qualifying matrices) is recorded per session (coverage.bands). STALL steers by onsets
(am. 5 / am. 15 (1)) and has no band.
Q am. 15 (5): when the target is a tier class, the session's initial state is drawn from the chosen matrix's stationary
distribution restricted to the class's band (coverage.initial_state_forced: the state, the restricted distribution, the
rule; orchestrate passes it as --initial-state); every other session keeps its matrix's start rule. Q am. 15 (6)'s
low_yield: a tier class whose best qualifying matrix, among those with >= LOW_YIELD_MIN_SESSIONS accepted sessions,
yields < LOW_YIELD_MIN_PER_SESSION minutes per session is listed in the coverage report (low_yield), with the qualifying
matrices not yet at that many sessions.
Q am. 17: a schedule family ("<m>@slow|fast", utils/shaping.SCHEDULES) qualifies for a tier class on its own occupancy
(schedule_occupancy.py: a seeded simulation of its exact schedule, committed with its sha256), against FLOOR_MINUTES /
its session's minutes; a young family is explored like any young matrix.
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
import random
from pathlib import Path

import numpy as np

from utils.shaping import PLATFORM_MATRICES, SCENARIOS, schedule_of, session_episodes, stationary_distribution

from data_root import DATA_ROOT as DATA_DIR  # noqa: E402  (one setting: data_root.py)
RULE_VERSION = "coverage-v2 (2026-10-18; Addendum Q am. 5: stall events)"
FLOOR_SESSIONS, FLOOR_MINUTES = 5, 2.0          # Addendum Q: N = 5 sessions with >= M = 2 minutes of every class
STALL_FLOOR_ONSETS = 3                          # am. 5: the STALL floor counts sessions with >= K = 3 stall onsets
TT_ENTRY_FLOOR = 5                              # Addendum Q: every tt shaper state at >= 5 room entries
STALL_BAND = {"youtube": (0.046, 0.183), "twitch": (0.029, 0.115), "tiktok": (0.037, 0.147)}
CLASSES = {"youtube": ["STALL", "Q_144p", "Q_240p", "Q_360p", "Q_480p", "Q_720p", "Q_1080p"],
           "twitch": ["STALL", "Q_144p", "Q_360p", "Q_480p", "Q_720p", "Q_1080p"],
           "tiktok": ["STALL", "Q_360p", "Q_480p", "Q_720p", "Q_1080p"]}
DEFAULT_MATRIX = {"youtube": "A", "tiktok": "TT-M", "twitch": "TW-M"}   # the no-deficit pick before Q am. 15 (2) (record)
ENTRY_EXCLUDED_CLASSES = ("not_live", "login_wall", "interrupted")   # amendment 3: the entry did not happen as designed
EXPLORE_MIN = 2
# the explore pick among young matrices (t53 2026-10-19, levi + supervisor, for the N = 4 count; at c1f9181 it took the
# sort-first young matrix, so twitch's TW-L was explored on every session while its failed sessions never counted):
# the fewest record attempts (any outcome, running included), ties by a seeded draw (seed EXPLORE_SEED + the
# platform's record attempts, recorded); a matrix whose last EXPLORE_REPEAT_CAP finished record sessions on the platform
# were all its own and none accepted is passed over while another matrix is eligible (recorded)
EXPLORE_RULE = "t53 2026-10-19: fewest record attempts, seeded tie draw, repeat cap"
EXPLORE_SEED = 0
EXPLORE_REPEAT_CAP = 2
FLAG_AFTER_SESSIONS = 5                         # supervisor 2026-10-18: a class with zero support after 5 sessions: FLAG
# Q am. 15 (3), revised 9dfbb62 (levi): the tier class -> state band rule and its inputs
STEERING_RULE = "Q am. 15 (2026-10-19; (3) revised at LVQoE 9dfbb62)"   # the matrix pick's rule; the floors stay RULE_VERSION
SESSION_EPISODES = {"youtube": 21, "tiktok": 23, "twitch": 23}   # = orchestrate.EPISODES_V2 (tested)
STATE_RATE = "midpoint"                         # a state's rate: "midpoint" of its range (am. 15 (3)), or "floor"
RUNG_MARGIN = {"declared_peak": 1.1, "declared_average": 1.4}
# kbit/s per tier class: (rung, kind, source). Medians over chaos sessions, read 2026-10-19 (peter's band table):
# yt: the played codec's (vp9) lowest adaptiveFormats bitrate at the height + the lowest audio, 10 sessions with an inline
# player response (yt_2..yt_40); twitch: master-playlist BANDWIDTH by short side (audio included), 24 sessions; tt: the
# HEVC ladder's sdk_params vbitrate (ld / sd / hd / uhd) in the room data, 34 sessions (uhd: 936 / 1080 short side).
_YT_SRC = "yt adaptiveFormats, vp9 + lowest audio, median of 10 chaos sessions (yt_2..yt_40), 2026-10-19"
_TW_SRC = "twitch master BANDWIDTH by short side, median of 24 chaos sessions, 2026-10-19"
_TT_SRC = "tt room stream_data sdk_params vbitrate, HEVC ladder, median over 34 chaos sessions, 2026-10-19"
RUNGS = {
    "youtube": {"Q_144p": (255.0, "declared_average", _YT_SRC), "Q_240p": (265.0, "declared_average", _YT_SRC),
                "Q_360p": (380.8, "declared_average", _YT_SRC), "Q_480p": (672.0, "declared_average", _YT_SRC),
                "Q_720p": (876.8, "declared_average", _YT_SRC), "Q_1080p": (3040.0, "declared_average", _YT_SRC)},
    "twitch": {"Q_144p": (230.0, "declared_peak", _TW_SRC), "Q_360p": (630.0, "declared_peak", _TW_SRC),
               "Q_480p": (1428.0, "declared_peak", _TW_SRC), "Q_720p": (3423.0, "declared_peak", _TW_SRC),
               "Q_1080p": (7065.9, "declared_peak", _TW_SRC)},
    "tiktok": {"Q_360p": (600.0, "declared_average", _TT_SRC), "Q_480p": (1000.0, "declared_average", _TT_SRC),
               "Q_720p": (1800.0, "declared_average", _TT_SRC), "Q_1080p": (3000.0, "declared_average", _TT_SRC)},
}
YT_RUNG_CODEC = "vp9"                           # am. 15 (3): yt's rung is the played codec's (vp9 on chaos)
TT_RUNG_REGIME = "hevc"                         # tt's rungs are the HEVC ladder's (levi: the regime the rung assumes)
GEOMETRY_FOR_CLASS = {("youtube", "Q_1080p"): "fullscreen:1920x1080"}   # am. 15 (3) / am. 2: the only yt geometry for it
SUMMARY_FILE = "_summaries/coverage_last.json"
INITIAL_STATE_RULE = ("Q am. 15 (5): a tier-class target starts in its band, drawn from the matrix's stationary "
                      "distribution restricted to the band")
LOW_YIELD_MIN_PER_SESSION, LOW_YIELD_MIN_SESSIONS = 0.5, 5   # Q am. 15 (6): minutes per session, over >= 5 sessions
MATRIX_TARGETS = {("youtube", "H"): ("Q_1080p",)}   # Q am. 15 (6): yt-H is drawn only for the Q_1080p target
# Q am. 17 (2026-10-20): the schedule families ("<m>@slow|fast", utils/shaping.SCHEDULES) each carry a floor of accepted
# sessions of record (a schedule-diversity floor, not a readable stratum); every deficit (class, entry state, family) is
# ranked on one scale, its share = missing / its floor; a family qualifies for a tier class on its own simulated
# occupancy (schedule_occupancy.py), never on the base matrix's stationary mass
FAMILY_FLOOR = 5
DEFICIT_RANK = "Q am. 17: deficit share = missing / floor, classes, entry states and families alike"


def drawable(platform: str, matrix_id: str, target_key: str | None) -> bool:
    """A matrix in MATRIX_TARGETS is drawn only for its targets (never by the no-deficit rotation); any other always."""
    only = MATRIX_TARGETS.get((platform, matrix_id))
    return only is None or target_key in only


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
        return {"minutes": {}, "stall_share": None, "buffer_s": None, "stall_onsets": 0, "stall_runs_s": []}
    ticks: dict = collections.Counter()
    buf, stall, runs, cur = [], False, [], 0
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
                cur += 1
                continue
            if cur:
                runs.append(cur)                   # am. 5: a stall event = a run of consecutive STALL slots
                cur = 0
            c = quality_class(_f(r.get("video_w")) or 0, _f(r.get("video_h")) or 0)
            if c:
                ticks[c] += 1
                b = _f(r.get("buffered_ahead_s"))
                if b is not None:
                    buf.append(b)
    if cur:
        runs.append(cur)
    n = sum(ticks.values())
    return {"minutes": {k: round(v / 60, 3) for k, v in sorted(ticks.items())},
            "stall_share": round(ticks["STALL"] / n, 4) if n else None, "buffer_s": _q(buf),
            "stall_onsets": len(runs), "stall_runs_s": runs}


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


def accepted_sessions(data_dir: Path, platform: str, select=None) -> list[Path]:
    """select(meta) -> bool: only the sessions it keeps (the trio: its sessions of record, levi 00265 note)."""
    out = []
    for chk in (Path(data_dir) / platform).glob("*/check.json"):
        c = _load(chk)
        if c.get("verdict") == "PASS" and (c.get("session_class") or "normal") == "normal":
            if select is not None and not select(_load(chk.parent / "meta.json")):
                continue
            out.append(chk.parent)
    return sorted(out)


def attempts(data_dir: Path, platform: str, select=None) -> dict:
    """Every session of `platform` (accepted, _failed or running; select(meta) keeps the sessions of record): per matrix
    the attempts, their total, and the last EXPLORE_REPEAT_CAP finished ones (check.json written or set aside in
    _failed) in start order as [matrix, accepted]."""
    root = Path(data_dir)
    acc = set(accepted_sessions(root, platform, select))
    rows = []
    for d in list((root / platform).glob("*")) + list((root / "_failed" / platform).glob("*")):
        meta = _load(d / "meta.json")
        if not meta or (select is not None and not select(meta)):
            continue
        done = (d / "check.json").exists() or d.parent.parent.name == "_failed"
        rows.append((float(meta.get("started_at") or 0.0), d.name, (meta.get("shaper") or {}).get("matrix_id") or "unknown",
                     d in acc, done))
    rows.sort()
    per = collections.Counter(m for _, _, m, _, _ in rows)
    recent = [[m, a] for _, _, m, a, done in rows if done][-EXPLORE_REPEAT_CAP:]
    return {"per_matrix": dict(sorted(per.items())), "total": len(rows), "recent": recent}


def explore_pick(young: list[str], snap: dict, others: bool) -> dict:
    """EXPLORE_RULE among the young matrices: the record of the pick; its matrix is None when every young matrix is
    capped and `others` (a matrix with yields) can take the pick."""
    att = snap.get("attempts") or {"per_matrix": {}, "total": 0, "recent": []}
    rec = att["recent"]
    capped = sorted({rec[0][0]} if len(rec) >= EXPLORE_REPEAT_CAP and len({m for m, _ in rec}) == 1
                    and not any(a for _, a in rec) else set())
    cand = [m for m in young if m not in capped]
    out = {"rule": EXPLORE_RULE, "young": young, "attempts": {m: att["per_matrix"].get(m, 0) for m in young},
           "capped": capped, "recent": rec}
    if not cand:
        if others:
            return {**out, "matrix": None, "why": "every young matrix capped: the yields decide"}
        cand, out["cap_overridden"] = list(young), True
    low = min(out["attempts"][m] for m in cand)
    tied = sorted(m for m in cand if out["attempts"][m] == low)
    seed = EXPLORE_SEED + int(att["total"])
    drawn = random.Random(seed).choice(tied) if len(tied) > 1 else tied[0]
    return {**out, "tied": tied, "seed": seed, "matrix": drawn}


def entry_states(meta: dict) -> list[str]:
    out = []
    for e in meta.get("entries") or []:
        st = ((e.get("first_shaped_after") or {}).get("state") if e.get("kind") == "stream"
              else (e.get("shaper_at_entry") or {}).get("state"))
        if st not in (None, "unshaped"):
            out.append(str(st))
    return out


def entry_sessions(data_dir: Path, platform: str, select=None) -> list[tuple[Path, dict]]:
    """(session dir, check.json) of every checked session, accepted or _failed, whose class is not excluded (and that
    select keeps, when given)."""
    out = []
    for c in sorted(list((Path(data_dir) / platform).glob("*/check.json"))
                    + list((Path(data_dir) / "_failed" / platform).glob("*/check.json"))):
        chk = _load(c)
        if chk and chk.get("session_class") not in ENTRY_EXCLUDED_CLASSES:
            if select is not None and not select(_load(c.parent / "meta.json")):
                continue
            out.append((c.parent, chk))
    return out


def targeted_class(meta: dict) -> str | None:
    """The tier class a session was steered at under am. 15 (5): its applied coverage record's class target, started
    in the band (initial_state_forced applied); else None."""
    cov = meta.get("coverage") or {}
    tgt = cov.get("target") or {}
    if cov.get("applied") and tgt.get("kind") == "class" and (cov.get("initial_state_forced") or {}).get("applied"):
        return tgt.get("key")
    return None


def snapshot(data_dir: Path, platform: str, select=None) -> dict:
    classes = CLASSES[platform]
    cnt, mins = dict.fromkeys(classes, 0), dict.fromkeys(classes, 0.0)
    st_min = lab_min = 0.0
    entries, codecs = collections.Counter(), collections.Counter()
    pm = collections.defaultdict(lambda: {"n": 0, "n_tier": 0, "min": collections.Counter(), "stall": 0.0, "lab": 0.0,
                                          "onsets": 0, "tg_n": collections.Counter(), "tg_min": collections.Counter()})
    onsets, run_s = 0, []
    sessions = accepted_sessions(data_dir, platform, select)
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
        m["onsets"] += sc["stall_onsets"]
        onsets += sc["stall_onsets"]
        run_s += sc["stall_runs_s"]
        tc = targeted_class(meta)
        if tc and tier_ok:                               # am. 15 (6): a session targeted at the class, band start (5)
            m["tg_n"][tc] += 1
            m["tg_min"][tc] += sc["minutes"].get(tc, 0.0)
        for c in classes:
            if c != "STALL" and not tier_ok:
                continue
            v = sc["minutes"].get(c, 0.0)
            mins[c] += v
            m["min"][c] += v
            cnt[c] += (sc["stall_onsets"] >= STALL_FLOOR_ONSETS) if c == "STALL" else (v >= FLOOR_MINUTES)
    out = {"platform": platform, "rule": RULE_VERSION, "n_sessions": len(sessions),
           "classes": {c: {"sessions_ge_min": cnt[c], "minutes": round(mins[c], 2),
                           "criterion": f">= {STALL_FLOOR_ONSETS} onsets" if c == "STALL" else f">= {FLOOR_MINUTES} min"}
                       for c in classes},
           "stall_onsets_pooled": onsets,
           "stall_run_s": {"n": len(run_s), **(_q(run_s) or {})},
           # am. 5: stall minutes and the pooled share are covariates now; the band is reported, not a target
           "stall_share_pooled": round(st_min / lab_min, 4) if lab_min else None, "stall_band": list(STALL_BAND[platform]),
           "per_matrix": {k: {"sessions": v["n"], "tier_sessions": v["n_tier"],
                              "class_minutes_mean": {c: round(v["min"][c] / (v["n"] if c == "STALL" else v["n_tier"]), 3)
                                                     if (v["n"] if c == "STALL" else v["n_tier"]) else None for c in classes},
                              "stall_onsets_mean": round(v["onsets"] / v["n"], 3) if v["n"] else None,
                              "targeted": {c: {"sessions": v["tg_n"][c], "class_minutes_mean": round(v["tg_min"][c] / v["tg_n"][c], 3)}
                                           for c in sorted(v["tg_n"])},
                              "stall_share": round(v["stall"] / v["lab"], 4) if v["lab"] else None}
                          for k, v in sorted(pm.items())}}
    out["attempts"] = attempts(data_dir, platform, select)
    if platform == "tiktok":
        es = entry_sessions(data_dir, platform, select)
        for sd, chk in es:
            entries.update(entry_states({"entries": (_load(sd / "meta.json").get("entries") or chk.get("entries") or [])}))
        out.update(tt_entries_by_state=dict(sorted(entries.items())), tt_entry_sessions=len(es),
                   tt_codec_sessions=dict(sorted(codecs.items())))
    return out


def state_rate(state: str) -> float:
    sc = SCENARIOS[state]
    return (sc.rate_min + sc.rate_max) / 2.0 if STATE_RATE == "midpoint" else float(sc.rate_min)


def class_band(platform: str, cls: str) -> dict | None:
    """Q am. 15 (3): the shaper states whose rate >= margin x the class's rung; None for a class without a rung (STALL)."""
    r = RUNGS.get(platform, {}).get(cls)
    if r is None:
        return None
    kbit, kind, src = r
    need = RUNG_MARGIN[kind] * kbit
    return {"band": sorted((s for s in SCENARIOS if state_rate(s) >= need), key=int), "rung_kbit": kbit, "kind": kind,
            "margin": RUNG_MARGIN[kind], "need_kbit": round(need, 1), "state_rate": STATE_RATE, "source": src,
            **({"codec": YT_RUNG_CODEC} if platform == "youtube" else {}),
            **({"regime": TT_RUNG_REGIME} if platform == "tiktok" else {})}


def state_mass(platform: str, matrices: dict, matrix_id: str) -> dict[str, float]:
    """The per-state mass a matrix qualifies on: a base matrix's stationary distribution; a schedule family's own
    simulated occupancy (Q am. 17, schedule_occupancy.py's committed table)."""
    if "@" in matrix_id:
        import schedule_occupancy
        return schedule_occupancy.occupancy(platform, matrix_id)
    return stationary_distribution(matrices[matrix_id][0])


def session_minutes(platform: str, matrix_id: str) -> float:
    """A session's shaped minutes under the matrix's schedule (the base: SESSION_EPISODES of 60 s)."""
    sc = schedule_of(matrix_id, base_episodes=SESSION_EPISODES[platform])
    return session_episodes(SESSION_EPISODES[platform], matrix_id) * sc["episode_s"] / 60.0


def band_map(platform: str, matrices: dict, ids: list[str]) -> dict:
    """Per tier class: its band and each allowed matrix's mass in it (state_mass), and the matrices that qualify (mass
    >= FLOOR_MINUTES / the session's minutes under the matrix's schedule, and drawable for the class: MATRIX_TARGETS)."""
    thr = FLOOR_MINUTES / float(SESSION_EPISODES[platform])
    fam = {m: FLOOR_MINUTES / session_minutes(platform, m) for m in ids if "@" in m}
    masses = {m: state_mass(platform, matrices, m) for m in ids}
    out = {}
    for cls in CLASSES[platform]:
        b = class_band(platform, cls)
        if b is None:
            continue
        mass = {m: round(sum(p for s, p in masses[m].items() if s in b["band"]), 4) for m in ids}
        out[cls] = {**b, "mass": mass, "min_mass": round(thr, 4),
                    "qualifies": [m for m in ids if mass[m] >= fam.get(m, thr) and drawable(platform, m, cls)]}
        if fam:
            out[cls]["min_mass_family"] = {m: round(v, 4) for m, v in sorted(fam.items())}
    return out


def _ranked(defs: list[dict]) -> list[dict]:
    """DEFICIT_RANK: the largest share first (ties: class order, states, families)."""
    for d in defs:
        d["share"] = round(d["deficit"] / d["floor"], 6)
    return sorted(defs, key=lambda d: (-d["share"], d["order"]))


def deficits(snap: dict, states: list[str] = (), families: list[str] = ()) -> list[dict]:
    """The floors' shortfalls, ranked by share (DEFICIT_RANK). families: the schedule family ids whose FAMILY_FLOOR
    counts (accepted sessions of record per family id)."""
    out = []
    for i, (c, v) in enumerate(snap["classes"].items()):
        if v["sessions_ge_min"] < FLOOR_SESSIONS:
            out.append({"kind": "class", "key": c, "deficit": float(FLOOR_SESSIONS - v["sessions_ge_min"]),
                        "floor": FLOOR_SESSIONS, "order": i})
    # am. 5: the stall-share band is retired as a target (reported in the snapshot only)
    if "tt_entries_by_state" in snap:
        for st in sorted(states, key=int):
            n = snap["tt_entries_by_state"].get(st, 0)
            if n < TT_ENTRY_FLOOR:
                out.append({"kind": "entry_state", "key": st, "deficit": float(TT_ENTRY_FLOOR - n),
                            "floor": TT_ENTRY_FLOOR, "order": 100 + int(st)})
    for j, m in enumerate(sorted(families)):
        n = (snap["per_matrix"].get(m) or {}).get("sessions", 0)
        if n < FAMILY_FLOOR:
            out.append({"kind": "schedule", "key": m, "deficit": float(FAMILY_FLOOR - n), "floor": FAMILY_FLOOR,
                        "order": 200 + j})
    return _ranked(out)


def pick_matrix(snap: dict, matrices: dict, allowed: list[str] | None = None, excluded_geometries: tuple = (),
                reserved: dict | None = None) -> dict:
    """The rule (module docstring) on a snapshot. matrices: the platform's {id: (transitions, low cap, init rule)}.
    excluded_geometries (t53 trio v1: ("fullscreen",)): a class whose only geometry (GEOMETRY_FOR_CLASS) is of an
    excluded mode is unreachable, recorded in unreachable_targets and geometry_excluded."""
    platform = snap["platform"]
    ids = sorted(allowed or matrices)
    states = sorted({s for m in ids for s in matrices[m][0]}, key=int) if platform == "tiktok" else []
    defs = deficits(snap, states, [m for m in ids if "@" in m])
    if reserved:   # t56: a class (am. 17: or a family) a running sibling slot already holds is that much nearer its floor
        for d in defs:
            if d["kind"] in ("class", "schedule") and reserved.get(d["key"]):
                d["deficit"] = float(d["deficit"]) - reserved[d["key"]]
        defs = _ranked([d for d in defs if d["deficit"] > 0])
    bands = band_map(platform, matrices, ids)
    # am. 15 (3): a tier class no allowed matrix can reach is not drawn; the next deficit is the target
    unreachable = [d["key"] for d in defs if d["kind"] == "class" and d["key"] in bands and not bands[d["key"]]["qualifies"]]
    no_geom = [d["key"] for d in defs if d["kind"] == "class" and d["key"] not in unreachable
               and (GEOMETRY_FOR_CLASS.get((platform, d["key"])) or "").split(":")[0] in excluded_geometries]
    unreachable += no_geom
    live = [d for d in defs if d["key"] not in unreachable]
    rec = {"platform": platform, "rule": RULE_VERSION, "steering_rule": STEERING_RULE, "deficit_rank": DEFICIT_RANK,
           "allowed": ids, "deficits": defs,
           "snapshot": snap, "bands": bands, "unreachable_targets": unreachable, "default_rotation": False, "geometry_forced": None}
    if excluded_geometries:
        rec["geometry_excluded"] = {"modes": list(excluded_geometries), "unreachable_by_geometry": no_geom}
    if reserved:
        rec["reserved"] = dict(reserved)
    pm = snap["per_matrix"]
    if not live:      # am. 15 (2): rotate, the least-collected allowed matrix first, ties to the lowest id
        rot = [m for m in ids if drawable(platform, m, None)] or ids         # am. 15 (6): never a target-only matrix
        n = {m: (pm.get(m) or {}).get("sessions", 0) for m in rot}
        m = min(rot, key=lambda x: (n[x], x))
        return {**rec, "matrix": m, "target": None, "yields": {}, "default_rotation": True,
                "why": "no deficit: rotation, least-collected matrix" + (" (every deficit unreachable)" if defs else "")}
    tgt = live[0]
    if tgt["kind"] == "schedule":                          # am. 17: a family's floor is met by the family itself
        return {**rec, "matrix": tgt["key"], "target": tgt, "yields": {}, "why": f"schedule floor {FAMILY_FLOOR}"}
    if tgt["kind"] == "class" and tgt["key"] in bands:     # only matrices that reach the class's band
        ids = bands[tgt["key"]]["qualifies"]
        rec["geometry_forced"] = GEOMETRY_FOR_CLASS.get((platform, tgt["key"]))
    else:                                                  # am. 15 (6): a target-only matrix, only for its targets
        ids = [m for m in ids if drawable(platform, m, tgt["key"])] or ids
    if tgt["kind"] == "entry_state":
        yields = {m: round(stationary_distribution(matrices[m][0]).get(tgt["key"], 0.0), 6) for m in ids}
        why = "stationary probability of the state"
    else:
        n_key = "sessions" if tgt["kind"] != "class" or tgt["key"] == "STALL" else "tier_sessions"
        young = [m for m in ids if (pm.get(m) or {}).get(n_key, 0) < EXPLORE_MIN]
        if young:
            ex = explore_pick(young, snap, others=len(young) < len(ids))
            rec["explore"] = ex
            if ex["matrix"] is not None:
                return {**rec, "matrix": ex["matrix"], "target": tgt, "yields": {},
                        "why": f"explore: fewer than {EXPLORE_MIN} accepted sessions on {young}"}
            ids = [m for m in ids if m not in young]          # every young matrix capped: the others' yields decide
        if tgt["key"] == "STALL":
            yields = {m: pm[m].get("stall_onsets_mean") or 0.0 for m in ids}
            why = "mean stall onsets per session"
        else:
            yields = {m: pm[m]["class_minutes_mean"][tgt["key"]] or 0.0 for m in ids}
            why = "mean minutes of the class per session"
    top = max(yields.values())
    return {**rec, "matrix": min(m for m in ids if yields[m] == top), "target": tgt, "yields": yields, "why": why}


def initial_state_forced(rec: dict, matrices: dict, rng=random) -> dict | None:
    """Q am. 15 (5): a tier-class target's initial state, drawn from the chosen matrix's stationary distribution
    restricted to the class's band; None for any other target (STALL, an entry state, none). The band's mass is > 0 for
    every qualifying matrix (it qualifies on that mass)."""
    tgt = rec.get("target") or {}
    b = (rec.get("bands") or {}).get(tgt.get("key")) if tgt.get("kind") == "class" else None
    if not b or rec.get("matrix") not in matrices:
        return None
    pi = stationary_distribution(matrices[rec["matrix"]][0])
    w = {s: pi[s] for s in sorted(b["band"], key=int) if pi.get(s, 0.0) > 0}
    if not w:
        return None
    tot = sum(w.values())
    state = rng.choices(list(w), weights=list(w.values()), k=1)[0]
    return {"state": state, "class": tgt["key"], "matrix": rec["matrix"], "band": b["band"],
            "distribution": {s: round(p / tot, 6) for s, p in w.items()}, "band_mass": round(tot, 4),
            "rule": INITIAL_STATE_RULE}


def choose(data_dir: Path, platform: str, allowed: list[str] | None = None, excluded_geometries: tuple = (),
           select=None, reserved: dict | None = None) -> dict:
    """orchestrate, before each session: the snapshot, the rule's choice for `platform` and, for a tier-class target,
    the forced initial state (Q am. 15 (5))."""
    rec = pick_matrix(snapshot(data_dir, platform, select), PLATFORM_MATRICES[platform], allowed, excluded_geometries,
                      reserved)
    return {**rec, "initial_state_forced": initial_state_forced(rec, PLATFORM_MATRICES[platform]),
            "taken_at": dt.datetime.now().isoformat(timespec="seconds")}


def low_yield(snap: dict, matrices: dict) -> list[dict]:
    """Q am. 15 (6): per tier class, its qualifying matrices (band_map over every defined matrix), each one's yield:
    over its >= LOW_YIELD_MIN_SESSIONS sessions targeted at the class under (5) (band start) when it has them (basis
    "targeted"; levi: that is the case the steering asks about, the all-sessions mean understates it), else over its
    >= LOW_YIELD_MIN_SESSIONS tier sessions (basis "untargeted"), else unmeasured. Listed when the best measured yield is
    below LOW_YIELD_MIN_PER_SESSION (a gap like yt_47's, visible), with each matrix's basis and the unmeasured ones."""
    pm, out = snap["per_matrix"], []
    for cls, b in band_map(snap["platform"], matrices, sorted(matrices)).items():
        ys, unmeasured = {}, {}
        for m in b["qualifies"]:
            v = pm.get(m) or {}
            tg = (v.get("targeted") or {}).get(cls) or {}
            if tg.get("sessions", 0) >= LOW_YIELD_MIN_SESSIONS:
                ys[m] = {"minutes": tg["class_minutes_mean"], "basis": "targeted", "sessions": tg["sessions"]}
            elif v.get("tier_sessions", 0) >= LOW_YIELD_MIN_SESSIONS:
                ys[m] = {"minutes": v["class_minutes_mean"].get(cls) or 0.0, "basis": "untargeted",
                         "sessions": v["tier_sessions"]}
            else:
                unmeasured[m] = {"tier_sessions": v.get("tier_sessions", 0), "targeted_sessions": tg.get("sessions", 0)}
        if not ys:
            continue
        best = min(ys, key=lambda m: (-ys[m]["minutes"], m))
        if ys[best]["minutes"] < LOW_YIELD_MIN_PER_SESSION:
            out.append({"class": cls, "best_matrix": best, "minutes_per_session": ys[best]["minutes"],
                        "basis": ys[best]["basis"], "sessions": ys[best]["sessions"], "yields": ys, "unmeasured": unmeasured})
    return out


def meta_record(rec: dict | None, platform: str, matrix_id: str | None, initial_state: str | None = None) -> dict | None:
    """exp.py: the coverage record for meta.json, marked applied iff it is for this platform and this matrix; its forced
    initial state (am. 15 (5)) applied iff the record is and the session started there (exp.py's --initial-state)."""
    if not rec:
        return None
    applied = rec.get("platform") == platform and rec.get("matrix") == matrix_id
    out = {**rec, "applied": applied}
    if rec.get("initial_state_forced"):
        out["initial_state_forced"] = {**rec["initial_state_forced"],
                                       "applied": applied and rec["initial_state_forced"].get("state") == initial_state}
    return out


def coverage_flags(snap: dict) -> list[str]:
    """FLAG lines: a class with no session >= FLOOR_MINUTES once the platform has FLAG_AFTER_SESSIONS accepted sessions."""
    p, n = snap["platform"], snap["n_sessions"]
    if n < FLAG_AFTER_SESSIONS:
        return []
    hevc = (snap.get("tt_codec_sessions") or {}).get("hevc", 0)
    return [f"FLAG {p}: {c} has zero support after {n} accepted sessions (none with {v['criterion']}; "
            f"{v['minutes']} min in all)" + (f"; tt tiers count HEVC sessions only: {hevc} of {n}" if p == "tiktok" and c != "STALL" else "")
            for c, v in snap["classes"].items() if v["sessions_ge_min"] == 0]


def regime_flags(data_dir: Path, platform: str) -> list[str]:
    """FLAG lines from check.json suspicious (stream_regime.py), accepted and _failed sessions: per flag, the sessions
    raising it, the first and the latest (by meta.json started_at, else the check.json write time; then the name)."""
    rows = []
    for c in list((Path(data_dir) / platform).glob("*/check.json")) + list((Path(data_dir) / "_failed" / platform).glob("*/check.json")):
        chk = _load(c)
        if chk:
            t = _load(c.parent / "meta.json").get("started_at")
            t = float(t) if isinstance(t, (int, float)) else c.stat().st_mtime     # session order: its start, else the check
            rows.append((t, c.parent.name, [x.get("flag") for x in chk.get("suspicious") or []]))
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
                               "deficits": deficits(snap, sorted(snap.get("tt_entries_by_state", {}), key=int),
                                                    [m for m in PLATFORM_MATRICES[p] if "@" in m]),
                               "low_yield": low_yield(snap, PLATFORM_MATRICES[p]),
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
        md += [f"**{p}**: {s['n_sessions']} accepted; stall onsets {s['stall_onsets_pooled']}, run s {s['stall_run_s']}; "
               f"stall share {s['stall_share_pooled']} (covariate; old band {s['stall_band']}); "
               f"buffer median over sessions {v['buffer_median_s_over_sessions']}"
               + (f"; latency to broadcaster {v['twitch_latency_median_s_over_sessions']}" if p == "twitch" else "")
               + (f"; codec sessions {s['tt_codec_sessions']} (tiers: hevc only); entries by state "
                  f"{s['tt_entries_by_state']} (over {s['tt_entry_sessions']} sessions, any class but not_live / login_wall / "
                  f"interrupted)" if p == "tiktok" else ""), "",
               "| class | sessions meeting the floor | criterion | minutes |", "|---|---|---|---|"]
        md += [f"| {c} | {x['sessions_ge_min']} | {x['criterion']} | {x['minutes']} |" for c, x in s["classes"].items()]
        md += ["", "deficits: " + (", ".join(f"{d['key']} {d['deficit']}" for d in v["deficits"]) or "none"),
               f"low_yield (best qualifying matrix < {LOW_YIELD_MIN_PER_SESSION} min / session over >= "
               f"{LOW_YIELD_MIN_SESSIONS} sessions): "
               + (", ".join(f"{x['class']} ({x['best_matrix']}: {x['minutes_per_session']} over {x['sessions']} "
                            f"{x['basis']})"
                            for x in v.get("low_yield", [])) or "none"), ""]
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
