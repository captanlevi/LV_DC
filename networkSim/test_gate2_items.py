#!/usr/bin/env python3
"""gate2_items (t53 gate 2 items 4a / 8, levi's checklist): playing intervals, the shot-free dropped-frame rate (+- 2 s
of every shot's [t_decide, ts]), counter resets, visibility at every tick, rendered fps per tier against the rendition's
fps (>= 0.9 x), and sessions from before the new columns. Synthetic labels.csv / shots/index.jsonl.

    python3 test_gate2_items.py        (exit 0 = pass)
"""
import csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate2_items as G  # noqa: E402

COLS = ["ts", "type", "video_w", "video_h", "dropped", "current_time", "paused", "is_ad", "visibility", "total_frames"]


def session(d, n=60, fps=30.0, drop_every=1, vis=None, shots=(), reset_at=None, paused_at=(), cols=COLS, h=720):
    sd = Path(d)
    (sd / "shots").mkdir(parents=True, exist_ok=True)
    rows, dropped, total = [], 0, 0
    for i in range(n):
        if reset_at is not None and i == reset_at:
            dropped, total = 0, 0                                     # a new <video> element
        elif i:
            dropped += drop_every
            total += fps
        r = {"ts": (1000.0 + i) * 1000, "type": "res_meta", "video_w": h * 16 // 9, "video_h": h, "dropped": dropped,
             "current_time": float(i), "paused": "True" if i in paused_at else "False", "is_ad": "False",
             "visibility": (vis or {}).get(i, "visible"), "total_frames": total}
        rows.append([r[c] for c in cols])
    with open(sd / "labels.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    (sd / "shots" / "index.jsonl").write_text("".join(json.dumps({"t_decide": 1000.0 + s, "ts": 1000.0 + s + 0.4}) + "\n"
                                                      for s in shots))
    return sd


def test_item8_shot_free_rate():
    with tempfile.TemporaryDirectory() as d:
        r = G.session_items(session(d, shots=(30,)))
        i8 = r["item8"]
        # 59 one-second intervals; the shot at [30, 30.4] +- 2 s touches intervals [28,29] .. [32,33] (strictly overlapping)
        assert i8["playing_s_all"] == 59.0 and i8["dropped_all"] == 59 and i8["dropped_per_playing_s_all"] == 1.0, i8
        assert i8["playing_s_shot_free"] == 54.0 and i8["dropped_shot_free"] == 54 and i8["shots"] == 1, i8
    with tempfile.TemporaryDirectory() as d:                          # a counter reset is a step of 0, never negative
        i8 = G.session_items(session(d, reset_at=20))["item8"]
        assert i8["dropped_all"] == 58 and i8["playing_s_all"] == 59.0, i8
    with tempfile.TemporaryDirectory() as d:                          # paused ticks bound no playing interval
        i8 = G.session_items(session(d, paused_at=(10, 11)))["item8"]
        assert i8["playing_s_all"] == 56.0, i8


def _d(fps):
    return {"Q_720p": {"delivered_fps": fps}}


def test_item4a_visibility_and_fps():
    with tempfile.TemporaryDirectory() as d:
        a = G.session_items(session(d), _d(30.0))["item4a"]
        assert a["visibility"] == {"recorded": True, "samples": 60, "not_visible": 0, "values": {"visible": 60},
                                   "first_not_visible_t": None, "ok": True}, a
        f = a["rendered_fps"]["Q_720p"]
        assert (f["fps_per_media_s_shot_free"], f["delivered_fps"], f["ratio"], f["ok"], a["ok"]) == (30.0, 30.0, 1.0, True, True), a
        assert (f["playback_rate_shot_free"], f["dropped_share_shot_free"]) == (1.0, round(1 / 30, 3)), f
        assert a["fps60_covered"] is False
    with tempfile.TemporaryDirectory() as d:                          # one hidden tick fails 4a
        a = G.session_items(session(d, vis={7: "hidden"}), _d(30.0))["item4a"]
        assert a["visibility"]["not_visible"] == 1 and a["visibility"]["first_not_visible_t"] == 1007.0 and a["ok"] is False
    with tempfile.TemporaryDirectory() as d:                          # 20 frames per media s of a 60 delivered: < 0.9 x
        a = G.session_items(session(d, fps=20.0), _d(60.0))["item4a"]
        assert a["rendered_fps"]["Q_720p"]["ok"] is False and a["ok"] is False and a["fps60_covered"] is True, a
        a = G.session_items(session(d, fps=26.0))["item4a"]           # no delivered fps: not read
        assert a["rendered_fps"]["Q_720p"]["ok"] is None and a["rendered_fps"]["Q_720p"]["note"] == "no delivered fps"
        assert a["ok"] is None
    with tempfile.TemporaryDirectory() as d:                          # 9 playing seconds: insufficient support, not read
        a = G.session_items(session(d, n=10, fps=20.0), _d(60.0))["item4a"]
        assert a["rendered_fps"]["Q_720p"]["note"] == "insufficient support" and a["ok"] is True, a
        assert a["fps60_covered"] is False
    with tempfile.TemporaryDirectory() as d:                          # a session from before the columns
        r = G.session_items(session(d, cols=COLS[:-2]))
        assert r["item4a"] == {"visibility": {"recorded": False, "ok": None}, "rendered_fps": {"recorded": False},
                               "fps60_covered": None, "ok": None}, r
        assert r["item8"]["playing_s_all"] == 59.0


def _tc(sd, seconds, rate=400.0, ratio=1.0, backlog=True, state="3", t0=1000.0):
    rows, b = [], 0
    for i in range(seconds + 1):
        rows.append({"ts": t0 + i, "in": {"rate_kbit": rate, "bytes": b, "drops": 0, "backlog": 1500 if backlog else 0}})
        b += int(rate * ratio * 1000 / 8)
    (Path(sd) / "tc_stats.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (Path(sd) / "net_labels.txt").write_text(json.dumps({"timestamp": t0 - 1, "state": state, "speed": rate}) + "\n")


def test_item5_byte_total_ratio():
    """levi (gate-2 call on pilot-1): per state over shaper-bound seconds, sum(bytes) x 8 / sum(rate x s) in [0.9, 1.15]."""
    for ratio, secs, backlog, want in ((1.0, 30, True, ("PASS", True)), (0.8, 30, True, ("FAIL (starvation while queued)", False)),
                                       (1.3, 30, True, ("above (reported)", True)), (1.0, 5, True, ("not shaper-bound", None)),
                                       (0.5, 30, False, (None, None))):
        with tempfile.TemporaryDirectory() as d:
            _tc(d, secs, ratio=ratio, backlog=backlog)
            r = G.item5(Path(d))
            if want[0] is None:                                         # never backlogged: nothing read
                assert r["per_state"] == {} and r["ok"] is None, r
                continue
            s = r["per_state"]["3"]
            assert (s["reading"], s["ok"]) == want and s["ratio"] == round(ratio, 3), (ratio, r)
    with tempfile.TemporaryDirectory() as d:
        assert G.item5(Path(d)) == {"recorded": False, "ok": None}


def test_items1_2_content_bands():
    """levi: a non-PASS whose every reason is a content band is reported (excluded as in single mode); else it gates."""
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        (sd / "verify.json").write_text(json.dumps({"verdict": "suspicious", "reasons": ["B/s outside old p10..p90 for ['360p']"]}))
        (sd / "check.json").write_text(json.dumps({"verdict": "PASS", "session_class": "normal", "reasons": []}))
        r = G.items1_2(sd)
        assert (r["1"]["ok"], r["1"]["reading"].startswith("content band only"), r["2"]["ok"]) == (None, True, True), r
        (sd / "verify.json").write_text(json.dumps({"verdict": "FAIL", "reasons": ["B/s outside old p10..p90 for ['360p']",
                                                                                   "chunk recall 0.40 vs logged responses"]}))
        (sd / "check.json").write_text(json.dumps({"verdict": "PASS", "session_class": "player_frozen", "reasons": []}))
        r = G.items1_2(sd)
        assert r["1"]["ok"] is False and r["2"]["ok"] is False, r


def test_cli():
    with tempfile.TemporaryDirectory() as d:
        sd = session(Path(d) / "tw_100001", shots=(30,))
        (Path(d) / "fps.json").write_text(json.dumps({"tw_100001": {"detail": {"Q_720p": {"delivered_fps": 60.0}}}}))
        G.main([str(sd), "--rendition-fps", str(Path(d) / "fps.json"), "--out", str(Path(d) / "o.json")])
        r = json.loads((Path(d) / "o.json").read_text())["sessions"][0]
        assert r["item4a"]["rendered_fps"]["Q_720p"]["delivered_fps"] == 60.0 and r["item4a"]["ok"] is False, r
        assert r["items1_2"]["1"]["reading"] == "verify.json missing" and r["item5"] == {"recorded": False, "ok": None}


def main() -> int:
    test_item8_shot_free_rate()
    test_item4a_visibility_and_fps()
    test_item5_byte_total_ratio()
    test_items1_2_content_bands()
    test_cli()
    print("test_gate2_items: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
