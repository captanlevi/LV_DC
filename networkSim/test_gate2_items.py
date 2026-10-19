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


def test_item4a_visibility_and_fps():
    with tempfile.TemporaryDirectory() as d:
        a = G.session_items(session(d), {"Q_720p": 30})["item4a"]
        assert a["visibility"] == {"recorded": True, "samples": 60, "not_visible": 0, "values": {"visible": 60},
                                   "first_not_visible_t": None, "ok": True}, a
        f = a["rendered_fps"]["Q_720p"]
        assert (f["fps_shot_free"], f["rendition_fps"], f["ok"], a["ok"]) == (30.0, 30, True, True), a
        assert (f["fps_per_media_s_shot_free"], f["playback_rate_shot_free"], f["dropped_share_shot_free"]) == \
            (30.0, 1.0, round(1 / 30, 3)), f
    with tempfile.TemporaryDirectory() as d:                          # one hidden tick fails 4a
        a = G.session_items(session(d, vis={7: "hidden"}), {"Q_720p": 30})["item4a"]
        assert a["visibility"]["not_visible"] == 1 and a["visibility"]["first_not_visible_t"] == 1007.0 and a["ok"] is False
    with tempfile.TemporaryDirectory() as d:                          # 26 fps rendered of a 30 fps rendition: < 0.9 x
        a = G.session_items(session(d, fps=26.0), {"Q_720p": 30})["item4a"]
        assert a["rendered_fps"]["Q_720p"]["ok"] is False and a["ok"] is False, a
        a = G.session_items(session(d, fps=26.0))["item4a"]           # no rendition fps: not read
        assert a["rendered_fps"]["Q_720p"]["ok"] is None and a["rendered_fps"]["Q_720p"]["note"] == "no rendition fps"
        assert a["ok"] is None
    with tempfile.TemporaryDirectory() as d:                          # a session from before the columns
        r = G.session_items(session(d, cols=COLS[:-2]))
        assert r["item4a"] == {"visibility": {"recorded": False, "ok": None}, "rendered_fps": {"recorded": False},
                               "ok": None}, r
        assert r["item8"]["playing_s_all"] == 59.0


def test_cli():
    with tempfile.TemporaryDirectory() as d:
        sd = session(Path(d) / "tw_100001", shots=(30,))
        (Path(d) / "fps.json").write_text(json.dumps({"tw_100001": {"tiers": {"Q_720p": 60}}}))
        G.main([str(sd), "--rendition-fps", str(Path(d) / "fps.json"), "--out", str(Path(d) / "o.json")])
        r = json.loads((Path(d) / "o.json").read_text())["sessions"][0]
        assert r["item4a"]["rendered_fps"]["Q_720p"]["rendition_fps"] == 60 and r["item4a"]["ok"] is False, r


def main() -> int:
    test_item8_shot_free_rate()
    test_item4a_visibility_and_fps()
    test_cli()
    print("test_gate2_items: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
