#!/usr/bin/env python3
"""t102 (B') (levi 23:34): tt's tier codec from WorkerAnchor's worker-MSE rows over the main element's presented frames.

Synthetic rvfc ticks + worker mse rows (worker time + meta time_origin):
  - an hevc main on a "mixed" ladder (tt_stream) -> tier_ok True; an h264 main after a rotation (a new worker) -> False,
    and the first-by-position mutant (the first video row's codec) would say hevc;
  - a mid-session switch: the codec covering the most presented frames decides, both directions (ti_100175's hvc1 ->
    avc1 changeType shape);
  - a preview element (MediaStream, no MSE) with fewer frames never becomes the main element;
  - row codecs: init's 'vide' sample entry (audio inits ignored), ct / add video mimes; worker rows only (a page MSE row,
    no wi, is not tt's); a worker without time_origin contributes nothing;
  - nulls: no ticks, no worker video rows, most frames before the first row;
  - record() reads meta.json's worker_anchor and the frames file; off: tier_ok reads tt_stream.codec (7809b21c).

    python3 test_tt_main.py        (exit 0 = pass)
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tt_main as M  # noqa: E402

T0 = 1_800_000_000.0
ORIGIN = {0: (T0 - 100) * 1000, 1: (T0 + 400) * 1000}         # worker wi -> time_origin (epoch ms)
WORKERS = [{"wi": w, "time_origin": o} for w, o in ORIGIN.items()]


def ticks(el, src, t0, n, fps=30):
    return [{"k": "tick", "el": el, "src": src, "ts": (t0 + i) * 1000, "pf": i * fps} for i in range(n)]


def wrow(wi, t_epoch, op, codec=None, kind="vide"):
    r = {"k": "mse", "wi": wi, "sb": 1, "op": op, "t": t_epoch * 1000 - ORIGIN[wi]}
    entry = {"hevc": "hvc1", "h264": "avc1"}.get(codec, codec)
    if op == "init":
        r["tr"] = [[1, 1000, kind, entry if kind == "vide" else "mp4a", 640, 1280, None]]
    else:
        r["mime"] = f"{'video' if kind == 'vide' else 'audio'}/mp4;codecs={ {'hevc': 'hev1.1.6.L93.B0', 'h264': 'avc1.64001f'}.get(codec, codec)}"
    return r


def _tier_on(meta: dict) -> bool:
    """tier_ok with the rule on (it ships off unless levi 23:34 (2) holds 9 / 9)."""
    keep = M.TT_MAIN_CODEC
    M.TT_MAIN_CODEC = True
    try:
        return M.tier_ok(meta)
    finally:
        M.TT_MAIN_CODEC = keep


def test_hevc_main_on_a_mixed_ladder():
    fr = [wrow(0, T0 + 9, "add", "hevc"), wrow(0, T0 + 9.5, "init", "hevc")] + ticks(2, "MediaSourceHandle#16", T0 + 10, 600)
    r = M.main_codec(fr, WORKERS)
    assert r["codec"] == "hevc" and r["element"] == [2, "MediaSourceHandle#16"] and r["frames_by_codec"] == {"hevc": 599 * 30}
    assert _tier_on({"tt_main_codec": r, "tt_stream": {"codec": "mixed"}})


def test_h264_main_after_a_rotation_and_the_position_mutant():
    fr = ([wrow(0, T0 + 9, "init", "hevc")] + ticks(2, "MediaSourceHandle#16", T0 + 10, 100)
          + [wrow(1, T0 + 409, "init", "h264")] + ticks(2, "MediaSourceHandle#40", T0 + 410, 700))
    r = M.main_codec(fr, WORKERS)
    assert r["codec"] == "h264" and r["element"] == [2, "MediaSourceHandle#40"] and not _tier_on({"tt_main_codec": r})
    assert M.timeline(fr, WORKERS)[0][1] == "hevc" != r["codec"]                  # the first-by-position mutant
    assert [s[1] for s in r["switches"]] == ["hevc", "h264"]


def test_mid_session_switch_majority_decides():
    fr = [wrow(0, T0 + 9, "add", "hevc"), wrow(0, T0 + 9.2, "init", "hevc")] + ticks(2, "MSH#16", T0 + 10, 1000)
    fr.insert(2, wrow(0, T0 + 17.5, "ct", "h264"))                               # ti_100175's shape: hvc1 -> avc1 at +8 s
    fr.insert(3, wrow(0, T0 + 17.6, "init", "h264"))
    r = M.main_codec(fr, WORKERS)
    assert r["codec"] == "h264" and r["frames_by_codec"]["hevc"] == 8 * 30 and r["switches"][1][1:] == ["h264", "ct"]
    back = [wrow(0, T0 + 9, "init", "h264"), wrow(0, T0 + 30, "ct", "hevc")] + ticks(2, "MSH#16", T0 + 10, 1000)
    r = M.main_codec(back, WORKERS)
    assert r["codec"] == "hevc" and _tier_on({"tt_main_codec": r})


def test_preview_element_never_main():
    fr = ([wrow(0, T0 + 9, "init", "h264")] + ticks(2, "MediaSourceHandle#5", T0 + 10, 600)
          + ticks(1, "MediaStream#3", T0 + 10, 50, fps=60))                         # a preview: MediaStream, fewer frames
    r = M.main_codec(fr, WORKERS)
    assert r["element"] == [2, "MediaSourceHandle#5"] and r["codec"] == "h264"


def test_row_codecs():
    assert M.row_codec(wrow(0, T0, "init", "hevc")) == "hevc" and M.row_codec(wrow(0, T0, "init", "h264")) == "h264"
    assert M.row_codec(wrow(0, T0, "init", "x", kind="soun")) is None                 # an audio init
    assert M.row_codec(wrow(0, T0, "ct", "hevc")) == "hevc" and M.row_codec(wrow(0, T0, "add", "h264")) == "h264"
    assert M.row_codec(wrow(0, T0, "add", "mp4a.40.2", kind="soun")) is None
    assert M.row_codec({"op": "init", "tr": [[1, 1000, "vide", "hev1", 1, 1, None]]}) == "hevc"
    page = {k: v for k, v in wrow(0, T0, "init", "hevc").items() if k != "wi"}       # a page MSE row: not a worker's
    assert M.timeline([page], WORKERS) == [] and M.timeline([wrow(0, T0, "init", "hevc")], [{"wi": 0}]) == []


def test_nulls():
    assert M.main_codec([], WORKERS)["why"] == "no rVFC ticks"
    fr = ticks(2, "MSH#1", T0 + 10, 100)
    assert M.main_codec(fr, WORKERS)["why"] == "no worker video init / changeType rows"
    late = fr + [wrow(0, T0 + 105, "init", "hevc")]
    r = M.main_codec(late, WORKERS)
    assert r["codec"] is None and r["why"] == "most frames precede every worker video row"


def test_record_reads_meta_and_frames(tmp_path: Path):
    fr = [wrow(0, T0 + 9, "init", "hevc")] + ticks(2, "MSH#16", T0 + 10, 60) + ["not json"]
    (tmp_path / "rvfc_frames.jsonl").write_text("".join((json.dumps(r) if isinstance(r, dict) else r) + "\n" for r in fr))
    (tmp_path / "meta.json").write_text(json.dumps({"frame_log": {"worker_anchor": {"workers": WORKERS}}}))
    assert M.record(tmp_path, "rvfc_frames.jsonl")["codec"] == "hevc"
    (tmp_path / "meta.json").write_text("{}")
    assert M.record(tmp_path, "rvfc_frames.jsonl")["why"] == "no worker video init / changeType rows"


def test_off_and_wiring():
    keep = M.TT_MAIN_CODEC
    try:
        M.TT_MAIN_CODEC = False
        assert M.tier_ok({"tt_stream": {"codec": "hevc"}, "tt_main_codec": {"codec": "h264"}})
        assert not M.tier_ok({"tt_stream": {"codec": "mixed"}, "tt_main_codec": {"codec": "hevc"}})
    finally:
        M.TT_MAIN_CODEC = keep
    assert "tier_ok = tt_main.tier_ok(meta)" in (HERE / "coverage_report.py").read_text()
    ex = (HERE / "exp.py").read_text()
    assert 'if platform == "tiktok" and tt_main.TT_MAIN_CODEC:' in ex
    assert 'tt_main_codec=tt_main.record(\n                    session_dir, session_files.side_file("frames")[0]))' in ex


def main() -> int:
    import tempfile
    for f in (test_hevc_main_on_a_mixed_ladder, test_h264_main_after_a_rotation_and_the_position_mutant,
              test_mid_session_switch_majority_decides, test_preview_element_never_main, test_row_codecs, test_nulls,
              test_off_and_wiring):
        f()
    with tempfile.TemporaryDirectory() as t:
        test_record_reads_meta_and_frames(Path(t))
    print("test_tt_main: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
