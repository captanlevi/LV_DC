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


def ent(kind, state):
    return ({"kind": kind, "shaper_at_entry": {"state": "unshaped"}, "first_shaped_after": {"state": state}}
            if kind == "stream" else {"kind": kind, "shaper_at_entry": {"state": state}})


def main():
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
        allc = [(c, 150) for c in C.CLASSES["youtube"]]
        for i in range(5):
            session(d, "youtube", f"a{i}", [c for c in allc if c[0] != "Q_1080p"] + [("Q_1080p", 30)], matrix="A")
        snap = C.snapshot(d, "youtube")
        defs = C.deficits(snap)
        assert defs[0]["key"] == "Q_1080p" and defs[0]["deficit"] == 5, defs
        r = C.pick_matrix(snap, PLATFORM_MATRICES["youtube"])
        assert r["matrix"] == "B" and r["why"].startswith("explore"), r              # B and current untried: B first
        for i in range(2):
            session(d, "youtube", f"b{i}", [("Q_1080p", 60)] + allc[:-1], matrix="B")
            session(d, "youtube", f"c{i}", [("Q_1080p", 200)] + allc[:-1], matrix="current")
        r = C.pick_matrix(C.snapshot(d, "youtube"), PLATFORM_MATRICES["youtube"])
        assert r["target"]["key"] == "Q_1080p" and r["matrix"] == "current", r       # most 1080p minutes per session
        assert C.pick_matrix(C.snapshot(d, "youtube"), PLATFORM_MATRICES["youtube"], ["A", "B"])["matrix"] == "B"
        r2 = C.pick_matrix(C.snapshot(d, "youtube"), PLATFORM_MATRICES["youtube"])
        assert json.dumps(r, sort_keys=True) == json.dumps(r2, sort_keys=True)      # deterministic
        tie = dict(snap, per_matrix={m: {"sessions": 3, "tier_sessions": 3, "stall_share": 0.1,
                                          "class_minutes_mean": dict.fromkeys(C.CLASSES["youtube"], 1.0)} for m in ("A", "B", "current")})
        assert C.pick_matrix(tie, PLATFORM_MATRICES["youtube"])["matrix"] == "A"     # equal yields: first id

    # stall band: over the band -> the lowest-stall matrix; all floors met -> default
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        allc = [(c, 150) for c in C.CLASSES["twitch"] if c != "STALL"]
        for i in range(3):
            session(d, "twitch", f"m{i}", allc + [("STALL", 400)], matrix="TW-M")
            session(d, "twitch", f"c{i}", allc + [("STALL", 130)], matrix="current")
        snap = C.snapshot(d, "twitch")
        assert snap["stall_share_pooled"] > C.STALL_BAND["twitch"][1]
        r = C.pick_matrix(snap, PLATFORM_MATRICES["twitch"])
        assert r["target"]["kind"] == "stall_high" and r["matrix"] == "TW-L" and r["why"].startswith("explore"), r
        r = C.pick_matrix(snap, PLATFORM_MATRICES["twitch"], ["TW-M", "current"])
        assert r["target"]["kind"] == "stall_high" and r["matrix"] == "current", r
        ok = dict(snap, stall_share_pooled=0.05, classes={c: {"sessions_ge_min": 5, "minutes": 50} for c in snap["classes"]})
        assert C.pick_matrix(ok, PLATFORM_MATRICES["twitch"])["matrix"] == "TW-M"

    # tt: tier floors count HEVC sessions only; stall and entry states count every accepted session
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        allc = [(c, 150) for c in C.CLASSES["tiktok"]]
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
        O.MATRIX_RULE = "fixed"
        assert O.session_matrix("tiktok") == O.MATRIX_IDS["tiktok"]
        assert json.loads(Path(os.environ["LVDC_COVERAGE_JSON"]).read_text())["rule"] == "fixed"

        # on-demand report and its table
        out = C.write(d, "2026-10-18")
        md = out.read_text()
        assert "| Q_720p | 1 |" in md and "codec sessions {'h264': 5, 'hevc': 1}" in md, md
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


if __name__ == "__main__":
    sys.exit(main())
