#!/usr/bin/env python3
"""t102 (B) (levi 23:22 / 23:24): tt's tier codec from the main player element.

Synthetic rvfc ticks + CDP media events: the main element is the one with the most presented frames; its player is the
video player with its first-frame size loaded in [first - 30 s, first + 2 s]; every candidate's codec must agree.
  - "mixed" ladder (tt_stream) with an hevc main -> tier_ok True; an h264 main (a rotated room) -> False;
  - hevc only on previews (fewer frames) -> False;
  - two candidates that agree -> that codec; that disagree -> null; a size-matching preview loaded in the window with
    another codec -> null, never the preview's codec;
  - no ticks / no first frame / no candidate -> null with a reason;
  - mutant: "the first video player by position" gives the wrong answer on the rotated-room case (the rule does not);
  - off: tier_ok reads tt_stream.codec (7809b21c); coverage_report's snapshot goes through tt_main.tier_ok.

    python3 test_tt_main.py        (exit 0 = pass)
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tt_main as M  # noqa: E402

T0 = 1_800_000_000.0


def ticks(el, src, t0, n, size=(720, 1280), fps=30):
    out = [{"k": "first_frame", "el": el, "src": src, "ts": t0 * 1000, "w": size[0], "h": size[1]}]
    out += [{"k": "tick", "el": el, "src": src, "ts": (t0 + i) * 1000, "pf": i * fps} for i in range(n)]
    return out


def player(pid, t_load, codec, size="720x1280"):
    return [{"t": t_load, "method": "playerEventsAdded", "playerId": pid,
             "events": [{"timestamp": 1.0, "value": json.dumps({"event": "kLoad", "url": f"blob:https://www.tiktok.com/{pid}"})}]},
            {"t": t_load + 0.5, "method": "playerPropertiesChanged", "playerId": pid,
             "properties": [{"name": "kVideoTracks", "value": json.dumps([{"codec": codec, "natural size": size}])}]}]


def test_hevc_main_on_a_mixed_ladder():
    fr = ticks(2, "MediaSourceHandle#16", T0 + 10, 600) + ticks(3, "MediaSourceHandle#20", T0 + 100, 60)
    me = player("A", T0 + 9.2, "hevc") + player("P", T0 + 99, "h264")          # P: a preview, fewer frames
    r = M.main_codec(fr, me)
    assert r["codec"] == "hevc" and r["element"] == [2, "MediaSourceHandle#16"] and r["frames"] == 599 * 30
    assert r["candidates"] == [{"player": "A", "codec": "hevc", "size": "720x1280", "dt": 0.8}] and r["agree"]
    assert M.tier_ok({"tt_main_codec": r, "tt_stream": {"codec": "mixed"}})


def rotated():
    """room 1 hevc (short), then a rotation: room 2's player h264 presents most frames."""
    fr = ticks(2, "MediaSourceHandle#16", T0 + 10, 120) + ticks(2, "MediaSourceHandle#40", T0 + 200, 700)
    me = player("R1", T0 + 9, "hevc") + player("R2", T0 + 199.7, "h264")
    return fr, me


def test_h264_main_after_a_rotation_and_the_position_mutant():
    fr, me = rotated()
    r = M.main_codec(fr, me)
    assert r["codec"] == "h264" and r["element"] == [2, "MediaSourceHandle#40"] and not M.tier_ok({"tt_main_codec": r})
    first_by_position = next(p["codec"] for p in M.players(me).values() if p["codec"])
    assert first_by_position == "hevc" != r["codec"]                              # the mutant would count it as tier


def test_hevc_only_on_previews():
    fr = ticks(2, "MediaSourceHandle#5", T0 + 10, 600) + ticks(5, "MediaSourceHandle#9", T0 + 50, 30)
    me = player("M", T0 + 9.5, "h264") + player("V", T0 + 49.5, "hevc")
    r = M.main_codec(fr, me)
    assert r["codec"] == "h264" and not M.tier_ok({"tt_main_codec": r})


def test_multi_candidates():
    fr = ticks(2, "MediaSourceHandle#58", T0 + 100, 600)
    agree = player("a", T0 + 72, "h264") + player("b", T0 + 86, "h264") + player("c", T0 + 98, "h264")
    r = M.main_codec(fr, agree)
    assert r["codec"] == "h264" and r["agree"] and sorted(c["dt"] for c in r["candidates"]) == [2.0, 14.0, 28.0]
    disagree = player("a", T0 + 86, "h264") + player("b", T0 + 98, "hevc")
    r = M.main_codec(fr, disagree)
    assert r["codec"] is None and r["why"] == "candidates disagree" and not r["agree"]
    assert not M.tier_ok({"tt_main_codec": r})


def test_size_matching_preview_in_the_window():
    fr = ticks(2, "MediaSourceHandle#16", T0 + 10, 600)
    me = player("main", T0 + 9, "hevc") + player("prev", T0 + 5, "h264")         # same size, inside the window
    r = M.main_codec(fr, me)
    assert r["codec"] is None and r["why"] == "candidates disagree"              # never the preview's codec
    other_size = player("main", T0 + 9, "hevc") + player("prev", T0 + 5, "h264", size="540x960")
    assert M.main_codec(fr, other_size)["codec"] == "hevc"


def test_window_edges_and_nulls():
    fr = ticks(2, "MediaSourceHandle#16", T0 + 10, 600)
    assert M.main_codec(fr, player("x", T0 + 10 + 1.9, "hevc"))["codec"] == "hevc"      # load after the frame (host t)
    assert M.main_codec(fr, player("x", T0 + 10 + 2.1, "hevc"))["codec"] is None
    assert M.main_codec(fr, player("x", T0 + 10 - 30.0, "hevc"))["codec"] == "hevc"
    r = M.main_codec(fr, player("x", T0 + 10 - 30.1, "hevc"))
    assert r["codec"] is None and r["why"] == "no 720x1280 video player loaded in the window"
    assert M.main_codec([], player("x", T0, "hevc")) == {**M.main_codec([], []), "codec": None}
    assert M.main_codec([], [])["why"] == "no rVFC ticks"
    no_first = [t for t in fr if t["k"] == "tick"]
    assert M.main_codec(no_first, player("x", T0 + 9, "hevc"))["why"] == "no first frame on the main element"


def test_record_reads_files(tmp_path: Path):
    tmp = tmp_path
    fr, me = rotated()
    (tmp / "rvfc_frames.jsonl").write_text("".join(json.dumps(r) + "\n" for r in fr) + "not json\n")
    (tmp / "media_events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in me))
    assert M.record(tmp, "rvfc_frames.jsonl", "media_events.jsonl")["codec"] == "h264"
    assert M.record(tmp, "absent.jsonl", "media_events.jsonl")["why"] == "no rVFC ticks"


def test_off_and_wiring():
    keep = M.TT_MAIN_CODEC
    try:
        M.TT_MAIN_CODEC = False
        assert M.tier_ok({"tt_stream": {"codec": "hevc"}, "tt_main_codec": {"codec": "h264"}})
        assert not M.tier_ok({"tt_stream": {"codec": "mixed"}, "tt_main_codec": {"codec": "hevc"}})
    finally:
        M.TT_MAIN_CODEC = keep
    cr = (HERE / "coverage_report.py").read_text()
    assert "tier_ok = tt_main.tier_ok(meta)" in cr
    ex = (HERE / "exp.py").read_text()
    assert 'if platform == "tiktok" and tt_main.TT_MAIN_CODEC:' in ex and "tt_main_codec=tt_main.record(" in ex
    assert M.WIN_BEFORE_S == 30.0 and M.WIN_AFTER_S == 2.0 and M.TT_MAIN_CODEC is True


def main() -> int:
    import tempfile
    for f in (test_hevc_main_on_a_mixed_ladder, test_h264_main_after_a_rotation_and_the_position_mutant,
              test_hevc_only_on_previews, test_multi_candidates, test_size_matching_preview_in_the_window,
              test_window_edges_and_nulls, test_off_and_wiring):
        f()
    with tempfile.TemporaryDirectory() as t:
        test_record_reads_files(Path(t))
    print("test_tt_main: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
