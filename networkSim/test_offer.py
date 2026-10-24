#!/usr/bin/env python3
"""collector v3 A4 (levi 16:48 / 16:49): the offered ladder. offer.tw_variants parses a twitch HLS master (EXT-X-MEDIA
NAME per VIDEO group, quoted CODECS with commas, audio-only variants left out); MasterListener.ladder records each master
read whose variants changed (meta.json tw_ladder, capped and counted); offer.offer_record gives one schema for every
platform (meta.json offer: video renditions of the first non-empty read, sorted by bitrate, short side for portrait tt;
missing stated, never an empty offer).

    python3 test_offer.py        (exit 0 = pass)
"""
import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    m = types.ModuleType("websocket")
    m.WebSocketTimeoutException = type("WebSocketTimeoutException", (Exception,), {})
    m.WebSocket = object
    sys.modules["websocket"] = m
import browser as B  # noqa: E402
import offer as O  # noqa: E402

MASTER = """#EXTM3U
#EXT-X-TWITCH-INFO:NODE="video-edge",MANIFEST-NODE-TYPE="weaver_cluster"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=8534030,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2",VIDEO="chunked",FRAME-RATE=60.000
https://example/chunked.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="720p60",NAME="720p60",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=3422999,RESOLUTION=1280x720,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="720p60",FRAME-RATE=60.000
https://example/720p60.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="160p30",NAME="160p",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=230000,RESOLUTION=284x160,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="160p30",FRAME-RATE=30.000
https://example/160p30.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="audio_only",NAME="audio_only",AUTOSELECT=NO,DEFAULT=NO
#EXT-X-STREAM-INF:BANDWIDTH=160000,CODECS="mp4a.40.2",VIDEO="audio_only"
https://example/audio_only.m3u8
"""


def test_tw_variants():
    v = O.tw_variants(MASTER)
    assert [x["name"] for x in v] == ["1080p60 (source)", "720p60", "160p"]          # audio-only left out
    assert v[0] == {"name": "1080p60 (source)", "bandwidth": 8534030, "resolution": "1920x1080",
                    "codecs": "avc1.64002A,mp4a.40.2", "frame_rate": 60.0}
    assert O.tw_variants("") == [] and O.tw_variants(None) == []


def test_master_listener_ladder():
    ml = B.MasterListener.__new__(B.MasterListener)
    ml.ladder_reads, ml.ladder_n_reads, ml.ladder_overflow, ml._ladder_last = [], 0, 0, None
    ml.ladder_errors, ml.ladder_last_error = 0, None
    url = "https://usher.ttvnw.net/api/channel/hls/x.m3u8?token=t"
    ml.ladder(MASTER, url, 1.0)
    ml.ladder(MASTER, url, 2.0)                                                       # unchanged: not recorded again
    ml.ladder(MASTER.replace("8534030", "8600000"), url, 3.0)
    ml.ladder("#EXTM3U\n", url, 4.0)                                                  # no variants: not a change
    r = ml.tw_ladder()
    assert r["n_reads"] == 4 and [x["t"] for x in r["reads"]] == [1.0, 3.0] and r["overflow"] == 0
    assert r["reads"][0]["path"] == "/api/channel/hls/x.m3u8"                         # no token in the record
    saved = B.WATCH_LOG_MAX
    try:
        B.WATCH_LOG_MAX = 1
        ml.ladder(MASTER.replace("8534030", "9000000"), url, 5.0)
        assert ml.tw_ladder()["overflow"] == 1
    finally:
        B.WATCH_LOG_MAX = saved


def test_malformed_master_is_counted():
    """levi 18:22: a malformed STREAM-INF raises in the parse; the listener counts it (errors + last_error) and goes on,
    and the offer reads missing with the error count."""
    bad = MASTER.replace("BANDWIDTH=8534030", "BANDWIDTH=lots")
    try:
        O.tw_variants(bad)
        raise AssertionError("a non-int BANDWIDTH must raise in the parser")
    except ValueError:
        pass
    src = (HERE / "browser.py").read_text()
    i = src.index("self.ladder(body, url, time.time())")
    assert "try:" in src[i - 120:i] and "self.ladder_errors += 1" in src[i:i + 200]
    off = O.offer_record("twitch", {"tw_ladder": {"n_reads": 2, "reads": [], "errors": 2, "last_error": "ValueError('x')"}})
    assert off["missing"] is True and off["read_errors"] == 2 and off["last_error"] == "ValueError('x')"


def test_offer_record():
    tw = O.offer_record("twitch", {"tw_ladder": {"n_reads": 3, "reads": [{"t": 10.0, "variants": O.tw_variants(MASTER)}]}})
    assert not tw["missing"] and tw["source"] == "hls master" and tw["t_first"] == 10.0 and tw["n_changes"] == 0
    assert [r["name"] for r in tw["renditions"]] == ["1080p60 (source)", "720p60", "160p"]
    assert tw["renditions"][0]["bitrate_kbit"] == 8534.0 and tw["renditions"][0]["short_side"] == 1080
    yt_read = {"t": 5.0, "formats": [
        {"itag": 140, "mimeType": 'audio/mp4; codecs="mp4a.40.2"', "bitrate": 130000},
        {"itag": 136, "mimeType": 'video/mp4; codecs="avc1.4d401f"', "bitrate": 2500000, "width": 1280, "height": 720,
         "qualityLabel": "720p", "fps": 30},
        {"itag": 137, "mimeType": 'video/mp4; codecs="avc1.640028"', "bitrate": 4500000, "width": 1920, "height": 1080,
         "qualityLabel": "1080p", "fps": 30}], "levels": ["hd1080", "hd720", "auto"], "ad": False}
    yt = O.offer_record("youtube", {"yt_ladder": {"n_reads": 9, "reads": [{"t": 1.0, "formats": []}, yt_read, yt_read]}})
    assert [r["name"] for r in yt["renditions"]] == ["1080p", "720p"] and yt["renditions"][1]["codecs"] == "avc1.4d401f"
    vp9 = {**yt_read, "formats": yt_read["formats"] + [{"itag": 247, "mimeType": 'video/webm; codecs="vp9"', "bitrate": 2500000,
                                                        "width": 1280, "height": 720, "qualityLabel": "720p", "fps": 30}]}
    two = O.offer_record("youtube", {"yt_ladder": {"reads": [vp9]}})["renditions"]
    assert sorted(r["codecs"] for r in two if r["name"] == "720p") == ["avc1.4d401f", "vp9"]   # brad 18:23: not collapsed
    assert yt["t_first"] == 5.0 and yt["n_changes"] == 1                              # the empty first read is skipped
    tt = O.offer_record("tiktok", {"tt_codec": {"reads": [{"t": 2.0, "h265": True, "renditions": [
        {"codec": "h264", "resolution": "720x1280", "vbitrate": "1800000", "suffix": "hd"},
        {"codec": "h264", "resolution": "1080x1920", "vbitrate": "3000000", "suffix": "or4"}]}]}})
    assert [r["name"] for r in tt["renditions"]] == ["or4", "hd"] and tt["renditions"][1]["short_side"] == 720
    none = O.offer_record("twitch", {"tw_ladder": None})
    assert none["missing"] is True and none["renditions"] == []
    assert O.offer_record("bilibili", {})["missing"] is True


def test_wiring():
    src = (HERE / "exp.py").read_text()
    assert 'meta_extra["offer"] = _offer.offer_record(platform, {k: meta_extra.get(k)' in src
    assert "tw_ladder=(browser._master.tw_ladder()" in src
    assert "self.ladder(body, url, time.time())" in (HERE / "browser.py").read_text()


def main() -> int:
    test_tw_variants()
    test_master_listener_ladder()
    test_malformed_master_is_counted()
    test_offer_record()
    test_wiring()
    print("test_offer: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
