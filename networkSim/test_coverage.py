#!/usr/bin/env python3
"""coverage_report (prereg Addendum Q, supervisor 2026-10-18): class minutes from labels.csv, the accepted-session
snapshot (tt tiers on HEVC sessions only), the deterministic matrix rule, the meta record, orchestrate's per-session
matrix and the daily-summary table. Synthetic session dirs (labels.csv / meta.json / check.json); no browser.

    python3 test_coverage.py        (exit 0 = pass)
"""
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coverage_report as C  # noqa: E402
import orchestrate as O  # noqa: E402
from utils.shaping import PLATFORM_MATRICES  # noqa: E402

COLS = ["ts", "type", "video_w", "video_h", "buffered_ahead_s", "is_ad"]
RES = {"Q_144p": (256, 144), "Q_240p": (426, 240), "Q_360p": (640, 360), "Q_480p": (854, 480), "Q_720p": (1280, 720),
       "Q_1080p": (1920, 1080)}


def session(root, platform, name, spans, verdict="PASS", s_class="normal", matrix="A", codec=None, entries=None, ads=0):
    """spans: [(class, seconds)], class STALL = a stall event then ticks, then a start event."""
    sd = Path(root) / platform / name
    sd.mkdir(parents=True)
    t, rows = 1.0e12, []
    for _ in range(ads):
        t += 1000
        rows.append([t, "res_meta", 640, 360, 5, "True"])
    for c, n in spans:
        if c == "STALL":
            rows.append([t, "stall", 640, 360, 0, "False"])
        for _ in range(n):
            t += 1000
            w, h = RES.get(c, (640, 360))
            rows.append([t, "res_meta", w, h, 0 if c == "STALL" else 10, "False"])
        if c == "STALL":
            rows.append([t, "start", 640, 360, 1, "False"])
    with open(sd / "labels.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(COLS)
        wr.writerows(rows)
    meta = {"platform": platform, "shaper": {"matrix_id": matrix}, "entries": entries or []}
    if codec:
        meta["tt_stream"] = {"codec": codec}
    (sd / "meta.json").write_text(json.dumps(meta))
    (sd / "check.json").write_text(json.dumps({"verdict": verdict, "session_class": s_class}))
    return sd


STALL3 = [("STALL", 50), ("Q_360p", 5), ("STALL", 50), ("Q_360p", 5), ("STALL", 50)]   # 3 onsets (am. 5 floor)


def ent(kind, state):
    return ({"kind": kind, "shaper_at_entry": {"state": "unshaped"}, "first_shaped_after": {"state": state}}
            if kind == "stream" else {"kind": kind, "shaper_at_entry": {"state": state}})


def test_am15_bands():
    """Q am. 15 (3), revised 9dfbb62: the band (midpoint rate >= margin x rung), the qualifying matrices (mass >= 2 min /
    episodes), the session episodes equal orchestrate's, the reading is one constant."""
    import orchestrate as O
    assert C.SESSION_EPISODES == O.EPISODES_V2
    assert C.state_rate("10") == 7500.0 and C.class_band("youtube", "STALL") is None
    b = C.class_band("youtube", "Q_1080p")
    assert (b["band"], b["need_kbit"], b["margin"], b["codec"]) == (["9", "10"], 4256.0, 1.4, "vp9"), b
    assert C.class_band("twitch", "Q_720p")["band"] == ["9", "10"] and C.class_band("twitch", "Q_1080p")["band"] == []
    assert C.class_band("tiktok", "Q_1080p")["band"] == ["9", "10"] and C.class_band("tiktok", "Q_720p")["regime"] == "hevc"
    m = C.band_map("youtube", PLATFORM_MATRICES["youtube"], ["A", "B", "current"])
    assert m["Q_1080p"]["qualifies"] == ["current"] and m["Q_720p"]["qualifies"] == ["A", "current"], m
    assert abs(m["Q_1080p"]["min_mass"] - 2 / 21) < 1e-3


def test_am15_initial_state_and_low_yield():
    """Q am. 15 (5): a tier-class target's initial state comes from the matrix's stationary distribution restricted to
    the class's band, recorded and marked applied only when the session started there; any other target keeps the
    matrix's start rule. (6): low_yield lists a class whose best qualifying matrix over >= 5 sessions yields < 0.5 min."""
    import random
    import orchestrate as O
    mats = PLATFORM_MATRICES["youtube"]
    bands = C.band_map("youtube", mats, sorted(mats))
    rec = {"platform": "youtube", "matrix": "current", "target": {"kind": "class", "key": "Q_1080p"}, "bands": bands}
    rng = random.Random(0)
    draws = [C.initial_state_forced(rec, mats, rng) for _ in range(400)]
    pi = C.stationary_distribution(mats["current"][0])
    want = pi["10"] / (pi["9"] + pi["10"])
    assert {d["state"] for d in draws} == {"9", "10"} and all(d["band"] == ["9", "10"] for d in draws)
    assert abs(sum(d["state"] == "10" for d in draws) / 400 - want) < 0.08, want
    d = draws[0]
    assert (d["class"], d["matrix"], d["rule"]) == ("Q_1080p", "current", C.INITIAL_STATE_RULE)
    assert abs(d["distribution"]["10"] - want) < 1e-5 and abs(d["band_mass"] - (pi["9"] + pi["10"])) < 1e-4
    for tgt in ({"kind": "class", "key": "STALL"}, {"kind": "entry_state", "key": "3"}, None):
        assert C.initial_state_forced({**rec, "target": tgt}, mats, rng) is None, tgt
    fr = {**rec, "initial_state_forced": d}
    assert C.meta_record(fr, "youtube", "current", d["state"])["initial_state_forced"]["applied"] is True
    assert C.meta_record(fr, "youtube", "current", None)["initial_state_forced"]["applied"] is False
    assert C.meta_record(fr, "youtube", "A", d["state"])["initial_state_forced"]["applied"] is False
    assert C.meta_record({**rec, "initial_state_forced": None}, "youtube", "current")["initial_state_forced"] is None
    # orchestrate passes the forced state as --initial-state (not the balance rule's); subprocess.run restored after
    seen, real_run = {}, O.subprocess.run
    try:
        O.subprocess.run = lambda cmd: seen.setdefault("cmd", cmd) and type("R", (), {"returncode": 0})()
        O._run_collection("youtube", d["state"], 21, auto=True, matrix="current")
    finally:
        O.subprocess.run = real_run
    assert seen["cmd"][seen["cmd"].index("--initial-state") + 1] == d["state"]
    src = (Path(__file__).resolve().parent / "orchestrate.py").read_text()
    assert 'initial_state=fi["state"] if fi else most_needed_state(' in src

    h, cap, rule = mats["H"]                                   # am. 15 (6) as recorded (LVQoE 57cd519e)
    assert (cap, rule) == (2, "stationary") and h["9"] == {"4": 0.05, "7": 0.05, "8": 0.15, "9": 0.45, "10": 0.30}
    assert h["10"] == {"3": 0.05, "8": 0.10, "9": 0.25, "10": 0.60} and all(abs(sum(v.values()) - 1) < 1e-9 for v in h.values())
    assert bands["Q_1080p"]["mass"]["H"] >= 0.25 and bands["Q_1080p"]["qualifies"] == ["H", "current"]

    def snap(n, minutes):
        cm = {c: 0.0 for c in C.CLASSES["youtube"]}
        return {"platform": "youtube", "per_matrix": {"current": {"sessions": n, "tier_sessions": n,
                                                                  "class_minutes_mean": {**cm, "Q_1080p": minutes}}}}
    ly = C.low_yield(snap(5, 0.33), mats)
    assert [x["class"] for x in ly if x["class"] == "Q_1080p"] == ["Q_1080p"], ly
    x = next(x for x in ly if x["class"] == "Q_1080p")
    assert (x["best_matrix"], x["minutes_per_session"], x["sessions"]) == ("current", 0.33, 5), x
    assert not any(x["class"] == "Q_1080p" for x in C.low_yield(snap(4, 0.33), mats))     # under 5 sessions: not yet
    assert not any(x["class"] == "Q_1080p" for x in C.low_yield(snap(5, 0.5), mats))
    assert x["basis"] == "untargeted" and x["unmeasured"] == {"H": {"tier_sessions": 0, "targeted_sessions": 0}}, x
    # levi: >= 5 sessions targeted at the class under (5) -> their mean (basis targeted), else the all-sessions mean
    s = snap(9, 0.3)
    s["per_matrix"]["current"]["targeted"] = {"Q_1080p": {"sessions": 5, "class_minutes_mean": 1.2}}
    assert not any(x["class"] == "Q_1080p" for x in C.low_yield(s, mats))
    s["per_matrix"]["current"]["targeted"] = {"Q_1080p": {"sessions": 5, "class_minutes_mean": 0.4}}
    x = next(x for x in C.low_yield(s, mats) if x["class"] == "Q_1080p")
    assert (x["basis"], x["minutes_per_session"], x["sessions"]) == ("targeted", 0.4, 5), x
    s["per_matrix"]["current"]["targeted"] = {"Q_1080p": {"sessions": 4, "class_minutes_mean": 1.2}}
    x = next(x for x in C.low_yield(s, mats) if x["class"] == "Q_1080p")
    assert (x["basis"], x["minutes_per_session"], x["sessions"]) == ("untargeted", 0.3, 9), x
    # the snapshot counts a session as targeted only with its record applied and its band start applied
    tgt = {"kind": "class", "key": "Q_1080p"}
    assert C.targeted_class({"coverage": {"applied": True, "target": tgt, "initial_state_forced": {"applied": True}}}) == "Q_1080p"
    for cov in ({"applied": False, "target": tgt, "initial_state_forced": {"applied": True}},
                {"applied": True, "target": tgt, "initial_state_forced": {"applied": False}},
                {"applied": True, "target": tgt}, {"applied": True, "target": {"kind": "class", "key": "STALL"}}, {}):
        assert C.targeted_class({"coverage": cov}) is None, cov
    with tempfile.TemporaryDirectory() as d:
        for i, (key, fi) in enumerate((("Q_1080p", True), ("Q_1080p", False), ("Q_720p", True))):
            sd = session(d, "youtube", f"h{i}", [("Q_1080p", 90), ("Q_720p", 30)], matrix="H")
            meta = json.loads((sd / "meta.json").read_text())
            meta["coverage"] = {"applied": True, "target": {"kind": "class", "key": key}, "initial_state_forced": {"applied": fi}}
            (sd / "meta.json").write_text(json.dumps(meta))
        tg = C.snapshot(Path(d), "youtube")["per_matrix"]["H"]["targeted"]
        assert tg == {"Q_1080p": {"sessions": 1, "class_minutes_mean": 1.5}, "Q_720p": {"sessions": 1, "class_minutes_mean": 0.5}}, tg


def test_select_sessions_of_record():
    """levi (note on 6c6ecca): the trio's steering counts its sessions of record only (record.is_record)."""
    import record
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for name, rec, era in (("tw_100001", False, record.ERA), ("tw_100002", True, record.ERA), ("tw_100003", True, "old")):
            sd = session(d, "twitch", name, [("Q_480p", 150)], matrix="TW-M")
            m = json.loads((sd / "meta.json").read_text())
            m["concurrency"] = {"mode": "trio", "record": rec, "era": era}
            (sd / "meta.json").write_text(json.dumps(m))
        assert [p.name for p in C.accepted_sessions(d, "twitch", record.is_record)] == ["tw_100002"]
        assert C.snapshot(d, "twitch", record.is_record)["n_sessions"] == 1 and C.snapshot(d, "twitch")["n_sessions"] == 3


def main():
    test_am15_bands()
    test_select_sessions_of_record()
    test_am15_initial_state_and_low_yield()
    # class rule = LVQoE quality_to_video_state on min(w, h)
    assert [C.quality_class(*r) for r in ((256, 144), (300, 200), (426, 240), (480, 300), (640, 360), (720, 420),
                                          (854, 480), (1080, 600), (1280, 720), (1600, 900), (1920, 1080), (1080, 1920), (0, 0))] == \
        ["Q_144p", "Q_144p", "Q_240p", "Q_240p", "Q_360p", "Q_360p", "Q_480p", "Q_480p", "Q_720p", "Q_720p", "Q_1080p",
         "Q_1080p", None]
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        sd = session(d, "youtube", "yt_1", [("Q_720p", 180), ("STALL", 30), ("Q_480p", 150)], ads=40)
        sc = C.session_classes(sd)
        assert sc["minutes"] == {"Q_480p": 2.5, "Q_720p": 3.0, "STALL": 0.5}, sc        # ad ticks left out
        assert sc["stall_share"] == round(30 / 360, 4) and sc["buffer_s"]["median"] == 10, sc
        assert sc["stall_onsets"] == 1 and sc["stall_runs_s"] == [30], sc              # am. 5: a run = one event
        sd3 = session(d, "youtube", "yt_9", [("Q_480p", 20), ("STALL", 4), ("Q_480p", 20), ("STALL", 2), ("Q_360p", 9),
                                             ("STALL", 7)], verdict="FAIL")
        assert (C.session_classes(sd3)["stall_onsets"], C.session_classes(sd3)["stall_runs_s"]) == (3, [4, 2, 7])
        session(d, "youtube", "yt_2", [("Q_1080p", 300)], verdict="FAIL")
        session(d, "youtube", "yt_3", [("Q_1080p", 300)], s_class="player_frozen")
        (d / "_failed" / "youtube").mkdir(parents=True)
        assert [p.name for p in C.accepted_sessions(d, "youtube")] == ["yt_1"]
        snap = C.snapshot(d, "youtube")
        assert snap["classes"]["Q_720p"]["sessions_ge_min"] == 1 and snap["classes"]["STALL"]["sessions_ge_min"] == 0
        assert snap["classes"]["Q_1080p"]["sessions_ge_min"] == 0                     # FAIL / non-normal not counted

    # the rule: the largest deficit first; explore matrices with < EXPLORE_MIN sessions; then the highest yield; ties by id
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        allc = STALL3 + [(c, 150) for c in C.CLASSES["youtube"] if c != "STALL"]
        for i in range(5):
            session(d, "youtube", f"a{i}", [c for c in allc if c[0] != "Q_1080p"] + [("Q_1080p", 30)], matrix="A")
        snap = C.snapshot(d, "youtube")
        defs = C.deficits(snap)
        assert defs[0]["key"] == "Q_1080p" and defs[0]["deficit"] == 5, defs
        r = C.pick_matrix(snap, PLATFORM_MATRICES["youtube"], ["A", "B", "current"])
        # Q am. 15 (3): of A / B / current only "current" puts >= 2/21 of its mass in Q_1080p's band (9-10); the target
        # couples fullscreen
        assert r["matrix"] == "current" and r["why"].startswith("explore"), r
        assert r["bands"]["Q_1080p"]["qualifies"] == ["current"] and r["geometry_forced"] == "fullscreen:1920x1080", r
        assert "geometry_excluded" not in r                                          # single mode: no new key
        # t53 trio v1 (supervisor 2026-10-19): no fullscreen -> Q_1080p unreachable, recorded; the next deficit drives
        rt = C.pick_matrix(snap, PLATFORM_MATRICES["youtube"], excluded_geometries=("fullscreen",))
        assert "Q_1080p" in rt["unreachable_targets"] and rt["geometry_forced"] is None, rt
        assert rt["geometry_excluded"] == {"modes": ["fullscreen"], "unreachable_by_geometry": ["Q_1080p"]}, rt
        assert (rt["target"] or {}).get("key") != "Q_1080p"
        # am. 15 (6): with yt-H defined it qualifies too; without sessions on either, the explore draws between them
        # (t53 2026-10-19: no attempts on either, a draw seeded by the platform's record attempts, 5, recorded)
        r = C.pick_matrix(snap, PLATFORM_MATRICES["youtube"])
        import random
        assert r["bands"]["Q_1080p"]["qualifies"] == ["H", "current"], r
        assert (r["explore"]["tied"], r["explore"]["seed"]) == (["H", "current"], 5), r["explore"]
        assert r["matrix"] == random.Random(5).choice(["H", "current"]) == r["explore"]["matrix"], r
        assert r["why"].startswith("explore") and r["geometry_forced"] == "fullscreen:1920x1080", r
        for i in range(2):
            session(d, "youtube", f"b{i}", [("Q_1080p", 60)] + allc[:-1], matrix="B")
            session(d, "youtube", f"c{i}", [("Q_1080p", 200)] + allc[:-1], matrix="current")
        r = C.pick_matrix(C.snapshot(d, "youtube"), PLATFORM_MATRICES["youtube"], ["A", "B", "current"])
        assert r["target"]["key"] == "Q_1080p" and r["matrix"] == "current", r       # most 1080p minutes per session
        rb = C.pick_matrix(C.snapshot(d, "youtube"), PLATFORM_MATRICES["youtube"], ["A", "B"])
        # A / B cannot reach 1080p: unreachable, no other deficit -> rotation, the least-collected (B: 2 vs A: 5)
        assert (rb["matrix"], rb["unreachable_targets"], rb["default_rotation"], rb["target"]) == ("B", ["Q_1080p"], True, None), rb
        r2 = C.pick_matrix(C.snapshot(d, "youtube"), PLATFORM_MATRICES["youtube"], ["A", "B", "current"])
        assert json.dumps(r, sort_keys=True) == json.dumps(r2, sort_keys=True)      # deterministic
        tie = dict(snap, per_matrix={m: {"sessions": 3, "tier_sessions": 3, "stall_share": 0.1, "stall_onsets_mean": 2.0,
                                          "class_minutes_mean": dict.fromkeys(C.CLASSES["youtube"], 1.0)} for m in ("A", "B", "current")})
        assert C.pick_matrix(tie, PLATFORM_MATRICES["youtube"], ["A", "B", "current"])["matrix"] == "current"   # the only one reaching 1080p
        # am. 15 (6): yt-H is drawn only for Q_1080p: never for another class (not even to explore), never by rotation
        tie_h = dict(tie, per_matrix={**tie["per_matrix"], "H": dict(tie["per_matrix"]["A"])})
        assert C.pick_matrix(tie_h, PLATFORM_MATRICES["youtube"])["matrix"] in ("H", "current")
        q720 = dict(snap, classes={**{c: {"sessions_ge_min": 5, "minutes": 50} for c in snap["classes"]},
                                   "Q_720p": {"sessions_ge_min": 0, "minutes": 0}})
        r = C.pick_matrix(q720, PLATFORM_MATRICES["youtube"])
        assert r["target"]["key"] == "Q_720p" and r["bands"]["Q_720p"]["qualifies"] == ["A", "current"], r
        assert r["matrix"] != "H" and r["bands"]["Q_720p"]["mass"]["H"] > 0.5, r       # it reaches the band, not drawn
        full = dict(snap, classes={c: {"sessions_ge_min": 5, "minutes": 50} for c in snap["classes"]})
        r = C.pick_matrix(full, PLATFORM_MATRICES["youtube"])
        assert r["default_rotation"] and r["matrix"] != "H", r                       # H has the fewest sessions (0)
        stall = dict(full, classes={**full["classes"], "STALL": {"sessions_ge_min": 0, "minutes": 0}})
        assert C.pick_matrix(stall, PLATFORM_MATRICES["youtube"])["matrix"] != "H"
        assert C.drawable("youtube", "H", "Q_1080p") and not C.drawable("youtube", "H", None)
        assert C.drawable("youtube", "A", None) and C.drawable("twitch", "TW-M", "STALL")

    # am. 5: STALL support = sessions with >= 3 onsets; minutes and the share are covariates (a long single stall and a
    # share far over the old band are no target); the STALL yield = mean onsets per session; all floors met -> default
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        allc = [(c, 150) for c in C.CLASSES["twitch"] if c != "STALL"]
        for i in range(3):
            session(d, "twitch", f"m{i}", allc + [("STALL", 400)], matrix="TW-M")                       # 1 onset each
            session(d, "twitch", f"c{i}", allc + [("STALL", 20), ("Q_480p", 5), ("STALL", 20)], matrix="current")
        snap = C.snapshot(d, "twitch")
        assert snap["stall_share_pooled"] > C.STALL_BAND["twitch"][1] and snap["classes"]["STALL"]["sessions_ge_min"] == 0
        assert snap["stall_onsets_pooled"] == 9 and snap["stall_run_s"]["n"] == 9, snap
        assert [d_["key"] for d_ in C.deficits(snap)] == ["STALL"]                   # the band is not a target
        r = C.pick_matrix(snap, PLATFORM_MATRICES["twitch"])
        assert r["target"]["key"] == "STALL" and r["matrix"] == "TW-L" and r["why"].startswith("explore"), r
        r = C.pick_matrix(snap, PLATFORM_MATRICES["twitch"], ["TW-M", "current"])
        assert r["matrix"] == "current" and r["yields"] == {"TW-M": 1.0, "current": 2.0}, r     # more onsets per session
        ok = dict(snap, classes={c: {"sessions_ge_min": 5, "minutes": 50, "criterion": ""} for c in snap["classes"]})
        r = C.pick_matrix(ok, PLATFORM_MATRICES["twitch"])          # Q am. 15 (2): no deficit -> the least-collected
        assert (r["matrix"], r["default_rotation"], r["target"]) == ("TW-L", True, None), r      # TW-L 0, others 3
        # Q am. 15 (3): twitch Q_1080p (1.1 x 7066 kbit/s) is above every state's midpoint: never drawn, reported;
        # the next deficit is the target
        q = dict(ok, classes={**ok["classes"], "Q_1080p": {"sessions_ge_min": 0, "minutes": 0, "criterion": ""},
                              "Q_480p": {"sessions_ge_min": 3, "minutes": 9, "criterion": ""}})
        r = C.pick_matrix(q, PLATFORM_MATRICES["twitch"])
        assert r["unreachable_targets"] == ["Q_1080p"] and r["target"]["key"] == "Q_480p", r
        assert set(r["bands"]["Q_480p"]["qualifies"]) == {"current", "TW-M"} and r["matrix"] in ("current", "TW-M"), r
        assert r["geometry_forced"] is None and r["bands"]["Q_1080p"]["band"] == []
        only = dict(ok, classes={**ok["classes"], "Q_1080p": {"sessions_ge_min": 0, "minutes": 0, "criterion": ""}})
        r = C.pick_matrix(only, PLATFORM_MATRICES["twitch"])     # every deficit unreachable -> rotation, reported
        assert r["default_rotation"] and r["unreachable_targets"] == ["Q_1080p"] and "unreachable" in r["why"], r

    # tt: tier floors count HEVC sessions only; stall and entry states count every accepted session
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        allc = STALL3 + [(c, 150) for c in C.CLASSES["tiktok"] if c != "STALL"]
        states = sorted({s for m in PLATFORM_MATRICES["tiktok"].values() for s in m[0]}, key=int)
        for i in range(5):
            session(d, "tiktok", f"h{i}", allc, matrix="TT-M", codec="h264",
                    entries=[ent("stream", s) for s in states] * 2)
        # amendment 3: entries count in every checked session but not_live / login_wall / interrupted ones
        session(d, "_failed/tiktok", "f0", [("Q_720p", 100)], verdict="FAIL", s_class="player_dead",
                entries=[ent("stream", states[0]), ent("room_reentry", states[1])])
        session(d, "_failed/tiktok", "f1", [], verdict="FAIL", s_class="startup_failure", entries=[ent("stream", states[0])])
        for k in ("not_live", "login_wall", "interrupted"):
            session(d, "_failed/tiktok", k, [], verdict="FAIL", s_class=k, entries=[ent("stream", states[0])] * 3)
        snap = C.snapshot(d, "tiktok")
        assert snap["classes"]["Q_720p"]["sessions_ge_min"] == 0 and snap["classes"]["STALL"]["sessions_ge_min"] == 5
        assert snap["n_sessions"] == 5 and snap["tt_entry_sessions"] == 7, snap
        assert snap["tt_entries_by_state"][states[0]] == 12 and snap["tt_entries_by_state"][states[1]] == 11, snap
        assert all(snap["tt_entries_by_state"][s] == 10 for s in states[2:]), snap
        assert snap["tt_codec_sessions"] == {"h264": 5}
        r = C.pick_matrix(snap, PLATFORM_MATRICES["tiktok"])
        assert r["target"]["key"] == "Q_360p" and r["why"].startswith("explore"), r  # no HEVC sessions on any matrix
        session(d, "tiktok", "v0", allc, matrix="TT-M", codec="hevc")
        snap = C.snapshot(d, "tiktok")
        assert snap["classes"]["Q_720p"]["sessions_ge_min"] == 1
        assert snap["per_matrix"]["TT-M"]["tier_sessions"] == 1 and snap["per_matrix"]["TT-M"]["sessions"] == 6
        few = dict(snap, classes={c: {"sessions_ge_min": 5, "minutes": 50} for c in snap["classes"]},
                   stall_share_pooled=0.08, tt_entries_by_state={s: (2 if s == states[0] else 9) for s in states})
        r = C.pick_matrix(few, PLATFORM_MATRICES["tiktok"])
        assert r["target"] == {"kind": "entry_state", "key": states[0], "deficit": 3.0, "order": 100 + int(states[0])}, r
        pi = {m: C.stationary_distribution(PLATFORM_MATRICES["tiktok"][m][0]).get(states[0], 0) for m in ("TT-M", "current")}
        assert r["matrix"] == max(pi, key=lambda m: (pi[m], m == "TT-M")), (r, pi)

        # meta record: applied iff this platform and this matrix
        assert C.meta_record({"platform": "tiktok", "matrix": "TT-M"}, "tiktok", "TT-M")["applied"] is True
        assert C.meta_record({"platform": "tiktok", "matrix": "TT-M"}, "tiktok", "current")["applied"] is False
        assert C.meta_record(None, "tiktok", "TT-M") is None

        # orchestrate: the rule's matrix reaches exp.py's flag and the record reaches LVDC_COVERAGE_JSON
        O._RUNS_DIR, O.MATRIX_RULE = d, "coverage"
        mid = O.session_matrix("tiktok")
        rec = json.loads(Path(os.environ["LVDC_COVERAGE_JSON"]).read_text())
        assert rec["matrix"] == mid and rec["platform"] == "tiktok" and rec["rule"] == C.RULE_VERSION, rec
        seen = {}
        O.subprocess.run = lambda cmd: seen.setdefault("cmd", cmd) and type("R", (), {"returncode": 0})()
        O._run_collection("tiktok", None, 23, auto=True, matrix="current")
        assert seen["cmd"][seen["cmd"].index("--tt-matrix") + 1] == "current", seen
        # Q am. 15 (3): a yt Q_1080p target couples the fullscreen geometry, overriding the rotation's draw
        O.LAST_COVERAGE["youtube"] = {"geometry_forced": "fullscreen:1920x1080"}
        assert O.coupled_geometry("youtube", "player:853x480") == "fullscreen:1920x1080"
        O.LAST_COVERAGE["youtube"] = {"geometry_forced": None}
        assert O.coupled_geometry("youtube", "player:853x480") == "player:853x480"
        assert O.coupled_geometry("twitch", "window:1920x1080") == "window:1920x1080"
        O.MATRIX_RULE = "fixed"
        assert O.session_matrix("tiktok") == O.MATRIX_IDS["tiktok"]
        assert json.loads(Path(os.environ["LVDC_COVERAGE_JSON"]).read_text())["rule"] == "fixed"

        # on-demand report and its table
        out = C.write(d, "2026-10-18")
        md = out.read_text()
        assert "| Q_720p | 1 |" in md and "codec sessions {'h264': 5, 'hevc': 1}" in md, md
        assert "low_yield (best qualifying matrix < 0.5 min / session over >= 5 sessions): " in md, md
        assert json.loads((d / "_summaries" / "coverage_2026-10-18.json").read_text())["platforms"]["tiktok"]["sessions"]

    # TW-L (Addendum Q amendment 3): the amendment's table, cap 2, stationary start in states 1-5, in the allowed set
    tl, cap, rule = PLATFORM_MATRICES["twitch"]["TW-L"]
    assert (cap, rule) == (2, "stationary") and tl["1"] == {"1": 0.50, "2": 0.35, "3": 0.15}
    assert tl["6"] == {"4": 0.40, "5": 0.40, "6": 0.20} and all(tl[k] == {"6": 1.0} for k in ("7", "8", "9", "10"))
    assert all(abs(sum(v.values()) - 1) < 1e-9 for v in tl.values())
    assert sum(v for k, v in C.stationary_distribution(tl).items() if int(k) <= 5) > 0.99

    # twitch latency covariate from player_stats.jsonl
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        with open(sd / "player_stats.jsonl", "w") as fh:
            for x in ("3.68 sec.", "4.10 sec.", "", None):
                fh.write(json.dumps({"stats": {"twitch_video_stats": {"Latency Mode": "Low Latency",
                                                                       "Latency To Broadcaster": x}}}) + "\n")
            fh.write("not json\n")
        lat = C.twitch_latency(sd)
        assert lat["modes"] == {"Low Latency": 4} and lat["latency_s"]["median"] == 3.89, lat
    print("test_coverage: ok")
    return 0


def test_main():
    """The script's own checks under pytest too (levi t58 T-1: the apply gate runs both runners). main() patches
    orchestrate's globals and subprocess.run for its own checks; they are restored for the tests after it."""
    import os
    import subprocess
    saved = (subprocess.run, O._RUNS_DIR, O.MATRIX_RULE, dict(O.LAST_COVERAGE), dict(os.environ))
    try:
        assert main() in (0, None)
    finally:
        subprocess.run, O._RUNS_DIR, O.MATRIX_RULE = saved[:3]
        O.LAST_COVERAGE.clear()
        O.LAST_COVERAGE.update(saved[3])
        os.environ.clear()
        os.environ.update(saved[4])


if __name__ == "__main__":
    sys.exit(main())
