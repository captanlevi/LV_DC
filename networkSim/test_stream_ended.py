#!/usr/bin/env python3
"""boundary bundle item 3 (levi f2fb3ee7, brad / levi 03:35): browser.StreamEndedWatch, the reader's stream_ended rule
applied live on yt / twitch. A yt tab falling back to the recording (yt_100047's shape) fires; a twitch raid fires on the
url alone; a yt /live -> watch?v=<live id> move on a live tab, a twitch ad break, a reload back to the live position and a
reload stuck at 0 (yt_100052's two shapes), and a directory page at entry never fire.

    python3 test_stream_ended.py        (exit 0 = pass)
"""
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

YT, TW = "https://www.youtube.com/@ch/live", "https://www.twitch.tv/chana"


def run(platform, channel, samples):
    w = B.StreamEndedWatch(platform, channel)
    for k, s in enumerate(samples):
        r = w.feed(s, 1000.0 + k)
        if r:
            return r, k
    return None, None


def yt_live(n, ct0=19480.0):
    return [{"url": YT, "ct": ct0 + k, "dur": 23080.3, "ad": False} for k in range(n)]


def test_yt_fallback_fires():
    vod = [{"url": "https://www.youtube.com/watch?v=abc", "ct": float(j), "dur": 3447.0 if j == 0 else 3446.621,
            "ad": False} for j in range(60)]
    r, k = run("youtube", YT, yt_live(500) + vod)
    assert r and r["reason"] == "stream_ended" and r["sign"] in ("U", "P") and r["sign_t"] == 1500.0, r
    assert 510 <= k <= 516, k                                     # confirmed once 10 fixed samples and 5 s of play
    assert r["from_page"] == "/@ch/live" and r["to_page"] == "watch:abc" or r["sign"] == "P", r


def test_twitch_raid_fires_on_the_url():
    raid = [{"url": "https://www.twitch.tv/chanb", "ct": 900.0 + k, "dur": "inf", "ad": False} for k in range(5)]
    live = [{"url": TW, "ct": 5000.0 + k, "dur": "inf", "ad": False} for k in range(300)]
    r, k = run("twitch", TW, live + raid)
    assert r and r["sign"] == "U" and k == 300 and r["to_page"] == "/chanb", (r, k)


def test_no_fire():
    # yt: /live -> watch?v=<the live id>, still live (duration inf, then the same DVR window)
    for dur in ("inf", 23080.3):
        moved = [{"url": "https://www.youtube.com/watch?v=live1", "ct": 19980.0 + k, "dur": dur, "ad": False}
                 for k in range(100)]
        assert run("youtube", YT, yt_live(500) + moved)[0] is None, dur
    # twitch: an ad break (is_ad, 30 s, the playhead from 0) and a reload (playhead to 0, duration still inf)
    live = [{"url": TW, "ct": 5000.0 + k, "dur": "inf", "ad": False} for k in range(300)]
    ad = [{"url": TW, "ct": float(j), "dur": 30.016, "ad": True} for j in range(30)]
    reload_ = [{"url": TW, "ct": float(j), "dur": "inf", "ad": False} for j in range(20)]
    assert run("twitch", TW, live + ad + live + reload_ + live)[0] is None
    # yt_100052: a reload at 0 with a finite duration for 6 s, then back to the live position; a reload stuck at 0
    back = [{"url": YT, "ct": 0.0, "dur": 844051.25, "ad": False}] * 6 + \
           [{"url": YT, "ct": 46890.0 + k, "dur": 844051.25, "ad": False} for k in range(80)]
    assert run("youtube", YT, yt_live(400) + back)[0] is None
    stuck = [{"url": YT, "ct": 0.0, "dur": 1637697.5, "ad": False}] * 90 + yt_live(30, 46850.0)
    assert run("youtube", YT, yt_live(400) + stuck)[0] is None
    # a directory page at entry, then the channel: no sign
    entry = [{"url": "https://www.twitch.tv/directory/category/x", "ct": None, "dur": None, "ad": False}] * 20
    assert run("twitch", TW, entry + live)[0] is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("test_stream_ended: ok")
