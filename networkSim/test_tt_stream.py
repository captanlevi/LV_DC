#!/usr/bin/env python3
"""tt_stream (supervisor 2026-10-18): the session's tt stream regime from http_v2 (synthetic room pages shaped like
chaos ti_36's: sdk_params per rendition with VCodec / stream_suffix / resolution, JSON-in-JSON with backslashes).

    python3 test_tt_stream.py        (exit 0 = pass)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tt_stream as T  # noqa: E402


def sdk(codec, suffix, res):
    return ('"flv":"https://x/stream-1_%s.flv?expire=1","sdk_params":"{\\"Auto\\":{\\"Demotion\\":{\\"StallCount\\":4},\\"Enable\\":1},'
            '\\"VCodec\\":\\"%s\\",\\"stream_suffix\\":\\"%s\\",\\"resolution\\":\\"%s\\"}"' % (suffix, codec, suffix, res))


def page(h264, hevc, button="false"):
    body = ('{"streamData":{"pull_data":{"options":{"show_quality_button":%s},"stream_data":"' % button
            + ",".join(sdk(*r) for r in h264) + '"}},"hevcStreamData":{"pull_data":{"stream_data":"'
            + ",".join(sdk(*r) for r in hevc) + '"}}}')
    return body + " " * 6000


def http(page_body, flv_suffixes):
    es = [{"request_url": "https://www.tiktok.com/@room/live?x=1", "response_data": page_body}]
    es += [{"request_url": f"https://pull/stream-1_{s}.flv?expire=1&sign=2"} for s in flv_suffixes]
    es += [{"request_url": "https://pull/stream-1.flv?only_audio=1"}]
    return {"conn": es}


def main() -> int:
    rooms = page([("h264", "hd", "1280x720"), ("h264", "ao", "")],
                 [("h265", "hd5", "1280x720"), ("h265", "sd5", "854x480"), ("h265", "ld5", "640x360")])
    r = T.regime(http(rooms, ["hd", "hd"]))                      # chaos so far: the H.264 single rung
    assert r["codec"] == "h264" and r["requested"] == {"hd": 2} and r["codec_of"] == {"hd": "h264"}, r
    assert r["ladder"] == {"h264": [["hd", "1280x720"]], "hevc": [["hd5", "1280x720"], ["ld5", "640x360"], ["sd5", "854x480"]]}, r
    assert r["show_quality_button"] == ["false"] and r["room_pages"] == 1
    r = T.regime(http(rooms, ["hd5", "sd5"]))                    # an HEVC session switching rungs
    assert r["codec"] == "hevc" and r["codec_of"] == {"hd5": "hevc", "sd5": "hevc"}, r
    assert T.regime(http(rooms, ["hd", "sd5"]))["codec"] == "mixed"
    assert T.regime(http(rooms, ["zz9"]))["codec"] == "unknown"
    nopage = {"conn": [{"request_url": "https://pull/stream-1_hd.flv?x=1"}]}           # ti_1: no room page captured
    r = T.regime(nopage)
    assert r["codec"] == "h264" and r["codec_inferred_from_suffix"] == ["hd"], r
    assert [T.suffix_codec(x) for x in ("hd", "ld", "hd5", "sld520", "uhd560", "ao", "")] == \
        ["h264", "h264", "hevc", "hevc", "hevc", "unknown", "unknown"]
    assert T.regime({})["codec"] == "unknown"
    assert T.tt_stream_regime("/nonexistent")["error"] == "no http_v2.json"
    print("test_tt_stream: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
