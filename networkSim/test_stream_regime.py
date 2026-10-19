#!/usr/bin/env python3
"""stream_regime (supervisor 2026-10-18): ladder / codec / player-size flags per platform on synthetic sessions shaped
like chaos's (yt player response qualityLabels + stats-for-nerds; twitch master playlist + Video Stats; tt room-page
sdk_params via tt_stream), orchestrate's per-session FLAG report and coverage_report's FLAG lines.

    python3 test_stream_regime.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coverage_report as C  # noqa: E402
import orchestrate as O  # noqa: E402
import stream_regime as S  # noqa: E402
from test_tt_stream import http as tt_http, page as tt_page  # noqa: E402


def sess(root, platform, name, http_v2=None, stats=(), meta=None, check=None):
    sd = Path(root) / platform / name
    sd.mkdir(parents=True)
    if http_v2 is not None:
        (sd / "http_v2.json").write_text(json.dumps(http_v2))
    with open(sd / "player_stats.jsonl", "w") as fh:
        for s in stats:
            fh.write(json.dumps({"stats": s}) + "\n")
    (sd / "meta.json").write_text(json.dumps(meta or {}))
    if check is not None:
        (sd / "check.json").write_text(json.dumps(check))
    return sd


def yt_http(labels):
    body = '{"streamingData":{"adaptiveFormats":[' + ",".join('{"itag":1,"qualityLabel":"%sp"}' % q for q in labels) + "]}}"
    return {"c1": [{"request_url": "/@chan/live", "response_data": body}]}


def nerds(cur, opt, codec="vp09.00.40.08 (247) / opus (251)", ad=-1):
    return {"ad_state": ad, "nerds": {"resolution": f"{cur}@30 / {opt}@30", "codecs": codec}}


def tw_master(rungs, codec="avc1.4D401F"):
    body = "#EXTM3U\n" + "".join(f'#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION={w}x{h},CODECS="{codec},mp4a.40.2",FRAME-RATE=30\n'
                                 f"https://x/{h}.m3u8\n" for w, h in rungs)
    return {"c1": [{"request_url": "/api/v2/channel/hls/chan.m3u8?x=1", "response_data": body}]}


def main():
    assert [S.codec_name(x) for x in ("avc1.4D401F", "hvc1.1.6", "hev1", "av01.0.01M.08", "vp09.00", "bytevc1", "", None)] == \
        ["h264", "hevc", "hevc", "av1", "vp9", "hevc", "unknown", "unknown"]
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # yt: 1080 offered, 481-px player -> reachable 720 < 1080 (chaos yt_2's shape); theater 1080-px -> no flag
        geo = {"geometry": {"player_client": [855, 481], "device_pixel_ratio": 1}}
        stats = [nerds("1280x720", "854x480")] * 5 + [nerds("1920x1080", "1920x1080", ad=1)]       # an ad record is ignored
        r = S.session_regime(sess(d, "youtube", "y1", yt_http(["144", "360", "480", "720", "1080"]), stats, geo), "youtube")
        assert r["regime"]["reachable_top"] == 720 and r["regime"]["played_top"] == 720, r
        assert S.flag_keys(r["suspicious"]) == ["player_below_top"], r
        th = {**geo, "player_pin": {"theater": {"player_device_px_h": 1009}}}
        r = S.session_regime(sess(d, "youtube", "y2", yt_http(["360", "480", "720", "1080"]), stats, th), "youtube")
        assert r["regime"]["reachable_top"] == 1080 and r["suspicious"] == [], r
        assert r["regime"]["ladder_source"] == "http_v2 player response", r
        r = S.session_regime(sess(d, "youtube", "y3", {"c": []}, stats, geo), "youtube")
        assert S.flag_keys(r["suspicious"]) == ["ladder_unknown"], r                   # no player response captured
        assert r["regime"]["ladder_why"] == ("no player response in http_v2 (the /live page is an app shell); "
                                             "player_stats has no quality_levels"), r
        # t37 (yt_41-43: the /live page is an app shell): the player API's levels, from non-ad ticks, give the ladder
        lv = [{**x, "quality_levels": ["hd1080", "hd720", "large", "medium", "small", "tiny", "auto"]} for x in stats[:5]]
        lv.append({**nerds("1920x1080", "1920x1080", ad=1), "quality_levels": ["hd2160", "auto"]})       # ad: ignored
        shell = {"c1": [{"request_url": "/@chan/live", "response_data": "<html>app shell</html>"}]}
        fs = {"geometry": {"player_client": [1920, 1080], "device_pixel_ratio": 1},
              "player_pin": {"fullscreen": {"player_device_px_h": 1080}, "theater": "not yt"}}
        r = S.session_regime(sess(d, "youtube", "y5", shell, lv, fs), "youtube")
        assert (r["regime"]["ladder"], r["regime"]["ladder_source"], r["regime"]["ladder_why"]) == \
            ([144, 240, 360, 480, 720, 1080], "player api levels", None), r
        assert r["regime"]["player_h_device_px"] == 1080 and r["regime"]["reachable_top"] == 1080, r
        r = S.session_regime(sess(d, "youtube", "y4", yt_http(["720", "1080"]), [nerds("1280x720", "1280x720", "hev1.1 (1)")],
                                  {"geometry": {"player_client": [1263, 710], "device_pixel_ratio": 1}}), "youtube")
        assert S.flag_keys(r["suspicious"]) == ["unexpected_codec", "short_ladder", "player_below_top"], r

        # twitch: a 5-rung H.264 ladder is expected; source-only and an HEVC delivery are flagged
        five = [(1920, 1080), (1280, 720), (852, 480), (640, 360), (284, 160)]
        vs = [{"twitch_video_stats": {"Codecs": "avc1.4D401F,mp4a.40.2"}}]
        r = S.session_regime(sess(d, "twitch", "t1", tw_master(five), vs), "twitch")
        assert r["regime"]["ladder"] == [160, 360, 480, 720, 1080] and r["suspicious"] == [], r
        r = S.session_regime(sess(d, "twitch", "t2", tw_master([(1920, 1080)]), vs), "twitch")
        assert S.flag_keys(r["suspicious"]) == ["single_rung_ladder"], r
        r = S.session_regime(sess(d, "twitch", "t3", tw_master(five, "hvc1.1.6.L120"), []), "twitch")
        assert r["regime"]["played_codec_source"] == "master_playlist" and S.flag_keys(r["suspicious"]) == ["unexpected_codec"], r

        # tt: chaos's regime (H.264 [hd] beside an HEVC ladder) -> unexpected_codec + single_rung_ladder; HEVC ladder -> none
        hevc = [("hevc", s, "1280x720") for s in ("hd5", "sd5", "ld5", "uhd560")]
        rooms = tt_page([("h264", "hd", "1280x720")], hevc)
        r = S.session_regime(sess(d, "tiktok", "k1", tt_http(rooms, ["hd"])), "tiktok")
        assert S.flag_keys(r["suspicious"]) == ["unexpected_codec", "single_rung_ladder"], r
        r = S.session_regime(sess(d, "tiktok", "k2", tt_http(rooms, ["sd5", "hd5"])), "tiktok")
        assert r["regime"]["rungs"] == 4 and r["suspicious"] == [], r
        meta = {"tt_stream": {"codec": "h264", "ladder": {}, "codec_inferred_from_suffix": ["hd"]}}   # ti_1: no room page
        r = S.session_regime(sess(d, "tiktok", "k3", None, (), meta), "tiktok")
        assert S.flag_keys(r["suspicious"]) == ["unexpected_codec", "ladder_unknown"], r
        assert S.session_regime(d / "nowhere", "bilibili")["suspicious"] == []

    # orchestrate: the first flagged session raises a FLAG with `new`; the next one with the same flags is not new
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        O._RUNS_DIR = d
        sus = [{"flag": "unexpected_codec"}, {"flag": "single_rung_ladder"}]
        sess(d, "tiktok", "ti_1", meta={"started_at": 1000.0}, check={"verdict": "PASS", "suspicious": sus})
        rows = O.report_flags("tiktok", 0)
        assert rows and rows[0]["new"] == ["unexpected_codec", "single_rung_ladder"], rows
        sess(d, "_failed/tiktok", "ti_2", meta={"started_at": 2000.0}, check={"verdict": "FAIL", "suspicious": sus[:1]})
        import time
        rows = O.report_flags("tiktok", time.time() - 5)
        assert {r["session"] for r in rows} == {"ti_1", "ti_2"} and all(r["new"] == [] for r in rows), rows
        assert len((d / O.FLAGS_FILE).read_text().splitlines()) == 3

        # coverage FLAG lines: a class with zero support after FLAG_AFTER_SESSIONS accepted sessions; regime flags per platform
        for i in range(C.FLAG_AFTER_SESSIONS):
            sd = sess(d, "tiktok", f"a{i}", meta={"tt_stream": {"codec": "h264"}, "shaper": {"matrix_id": "TT-M"},
                                                  "started_at": 3000.0 + i},
                      check={"verdict": "PASS", "session_class": "normal", "suspicious": sus})
            (sd / "labels.csv").write_text("ts,type,video_w,video_h,is_ad\n" + "".join(f"{1e12 + k * 1e3},res_meta,1280,720,False\n" for k in range(200)))
        md = "\n".join(C.markdown(C.report(d, ("tiktok",))))
        assert f"FLAG tiktok: Q_720p has zero support after {C.FLAG_AFTER_SESSIONS + 1} accepted sessions" in md, md
        assert "tt tiers count HEVC sessions only: 0 of 6" in md and "FLAG tiktok: STALL has zero support" in md, md
        assert "FLAG tiktok regime: unexpected_codec on 7 of 7 checked sessions (first ti_1" in md, md
        assert C.coverage_flags(dict(C.snapshot(d, "tiktok"), n_sessions=4)) == []
    print("test_stream_regime: ok")
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
