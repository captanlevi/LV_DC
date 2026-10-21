#!/usr/bin/env python3
"""yt's ladder, record-only (levi 16:26 / 16:27, the 10-22 tip): browser.YT_LADDER_JS reads the page-held formats
(the player response's adaptiveFormats, codecs in mimeType) and getAvailableQualityLevels(), never sets a quality;
browser.YtLadderRecorder writes a read only when it changed (with the ad flag, brad 16:27), counts null reads, keeps
each call's wall time (meta.json stream_read_timing.yt_ladder) and caps the reads at WATCH_LOG_MAX.

    python3 test_yt_ladder.py        (exit 0 = pass)
"""
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402

STUB = r"""
const [hasPlayer, ad] = JSON.parse(process.argv[2]);
const calls = [];
const p = {getPlayerResponse() { calls.push('getPlayerResponse'); return {streamingData: {adaptiveFormats: [
              {itag: 299, mimeType: 'video/mp4; codecs="avc1.64002a"', bitrate: 6000000, width: 1920, height: 1080,
               qualityLabel: '1080p60', fps: 60, url: 'https://secret'},
              {itag: 140, mimeType: 'audio/mp4; codecs="mp4a.40.2"', bitrate: 130000}]}}; },
           getAvailableQualityLevels() { calls.push('getAvailableQualityLevels'); return ['hd1080', 'hd720', 'auto']; },
           setPlaybackQuality() { calls.push('SET'); }, setPlaybackQualityRange() { calls.push('SET'); }};
global.window = {};
global.document = {documentElement: {dataset: {lvqoeAd: ad ? '1' : '0'}},
                   querySelector: (q) => (hasPlayer && q === '#movie_player') ? p : null};
const r = eval(process.argv[1]);
console.log(JSON.stringify({r, calls}));
"""


def js(has_player=True, ad=False):
    out = subprocess.run([shutil.which("node"), "-e", STUB, B.YT_LADDER_JS, json.dumps([has_player, ad])],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_never_sets_a_quality():
    """levi 16:27: no setPlaybackQuality / setPlaybackQualityRange on the yt_ladder path (the JS and the loop's read)."""
    src = (HERE / "browser.py").read_text()
    loop = src[src.index("if yt_ladder is not None and yt_ladder.due"):src.index("if on_wall is not None and not walled")]
    for text in (B.YT_LADDER_JS, loop, B.YtLadderRecorder.feed.__doc__ or ""):
        assert not re.search(r"setPlaybackQuality", text), text[:200]
    assert "click" not in B.YT_LADDER_JS and "fetch" not in B.YT_LADDER_JS


def test_recorder():
    assert B.YT_LADDER_EVERY_S == 30.0
    r = B.YtLadderRecorder()
    a = json.dumps({"formats": [{"itag": 299, "height": 1080}], "levels": ["hd1080", "auto"], "ad": False})
    b = json.dumps({"formats": [{"itag": 299, "height": 1080}, {"itag": 136, "height": 720}], "levels": ["hd1080"],
                    "ad": False})
    assert r.due(0.0)
    r.feed(a, 10.0, 1.5)
    assert not r.due(39.9) and r.due(40.0)                       # every 30 s
    r.feed(a, 40.0, 2.5)                                          # unchanged: not written
    r.feed(None, 70.0)                                            # no player yet / a failed read: counted, not written
    r.feed(json.dumps({"formats": [], "levels": None}), 100.0)    # an empty read: null
    r.feed(b, 130.0, 0.5)                                         # changed: written
    rec = r.record()
    assert rec["n_reads"] == 5 and rec["n_null"] == 2 and [x["t"] for x in rec["reads"]] == [10.0, 130.0], rec
    assert rec["reads"][0]["ad"] is False and rec["reads"][1]["levels"] == ["hd1080"]
    t = r.timing()
    assert t["n"] == 3 and t["max_ms"] == 2.5 and t["p50_ms"] == 1.5, t
    ad = json.dumps({"formats": [{"itag": 1, "height": 360}], "levels": ["medium"], "ad": True})
    r.feed(ad, 160.0)                                             # brad 16:27: an ad's response is written, marked
    assert r.record()["reads"][-1]["ad"] is True
    cap = B.YtLadderRecorder()
    for k in range(B.WATCH_LOG_MAX + 20):
        cap.feed(json.dumps({"formats": [{"itag": k}], "levels": None}), float(k))
    assert len(cap.record()["reads"]) == B.WATCH_LOG_MAX and cap.record()["n_reads"] == B.WATCH_LOG_MAX + 20


def test_js():
    out = js()
    v = json.loads(out["r"])
    assert out["calls"] == ["getPlayerResponse", "getAvailableQualityLevels"], out   # reads only, never SET
    assert v["levels"] == ["hd1080", "hd720", "auto"] and v["ad"] is False
    assert v["formats"][0] == {"itag": 299, "mimeType": 'video/mp4; codecs="avc1.64002a"', "bitrate": 6000000,
                               "width": 1920, "height": 1080, "qualityLabel": "1080p60", "fps": 60}   # no url kept
    assert json.loads(js(ad=True)["r"])["ad"] is True
    nop = json.loads(js(has_player=False)["r"])
    assert nop == {"formats": [], "levels": None, "ad": False}, nop


TT_STUB = r"""
const [text, flag] = JSON.parse(process.argv[2]);
global.localStorage = {getItem: (k) => k === 'webcodecs_ability_key_temporary' && flag !== null
                       ? JSON.stringify({isSupportH265: flag}) : null};
global.document = {scripts: [{textContent: 'x'}, {textContent: text}]};
console.log(JSON.stringify({r: eval(process.argv[1])}));
"""


def tt_js(text, flag=True):
    out = subprocess.run([shutil.which("node"), "-e", TT_STUB, B.TT_CODEC_JS, json.dumps([text, flag])],
                         capture_output=True, text=True, check=True)
    return json.loads(json.loads(out.stdout)["r"])


def test_tt_codec_js():
    """levi 18:36 (A): the page-held capability flag and the sdk_params renditions (test_tt_stream's room shape:
    JSON-in-JSON with backslashes), parsed in the page; read only, no injection."""
    import test_tt_stream as TS
    room = TS.page([("h264", "hd", "1280x720"), ("h264", "ao", "")],
                   [("h265", "hd5", "1280x720"), ("h265", "sd5", "854x480")])
    v = tt_js(room)
    assert v["h265"] is True and {(r["codec"], r["suffix"], r["resolution"]) for r in v["renditions"]} == {
        ("h264", "hd", "1280x720"), ("h264", "ao", ""), ("h265", "hd5", "1280x720"), ("h265", "sd5", "854x480")}, v
    assert tt_js("no room data here", None) == {"h265": None, "renditions": []}
    for word in ("setPlaybackQuality", ".configure", "prototype", "fetch(", "click"):
        assert word not in B.TT_CODEC_JS, word


def test_tt_codec_recorder():
    r = B.TtCodecRecorder()
    assert B.TT_CODEC_EVERY_S == 30.0 and r.every_s == 30.0
    a = json.dumps({"h265": True, "renditions": [{"codec": "h265", "resolution": "1280x720", "vbitrate": "1", "suffix": "hd5"}]})
    r.feed(a, 1.0, 3.0)
    r.feed(a, 31.0, 2.0)                                           # unchanged
    r.feed(json.dumps({"h265": None, "renditions": []}), 61.0)     # null
    rec = r.record()
    assert rec["n_reads"] == 3 and rec["n_null"] == 1 and len(rec["reads"]) == 1 and rec["reads"][0]["h265"] is True
    assert rec["reads"][0]["renditions"][0]["suffix"] == "hd5" and "decode_hw" in rec["rule"] and r.timing()["n"] == 2


def main() -> int:
    test_never_sets_a_quality()
    test_recorder()
    test_tt_codec_recorder()
    if not shutil.which("node"):
        print("SKIP (js): node not found")
        return 0
    test_js()
    test_tt_codec_js()
    print("test_yt_ladder: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
