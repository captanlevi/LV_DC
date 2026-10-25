#!/usr/bin/env python3
"""item #9 (record only): TtRoomListener reads tt's own /webcast/room/enter/ responses over CDP (as MasterListener reads
twitch's master: Network on the page and its workers, Network.getResponseBody on loadingFinished, no request of ours) and
records meta.json tt_room_enter: the offered qualities (stream_data's sdk_params, a JSON string inside a JSON string),
options.qualities, and the multi-guest fields with a derived multi_guest and the conditions that fired; on change per
path, capped and counted; a malformed body is counted, never fatal; no body, no URL in the record. Synthetic CDP messages
and a synthetic body in the W-B structure; no browser, no network.

    python3 test_tt_room_enter.py        (exit 0 = pass)
"""
import base64
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

ENTER_URL = "https://webcast.tiktok.com/webcast/room/enter/?aid=1988&room_id=7&msToken=SECRET"


def _q(vcodec, res, vbr, **extra):
    sp = {"VCodec": vcodec, "resolution": res, "vbitrate": vbr, "AudioCodec": "aac", **extra}
    return {"main": {"flv": "https://pull-flv/stream.flv?expire=1&sign=SECRET", "hls": "https://pull-hls/x.m3u8",
                     "sdk_params": json.dumps(sp)}}


def body(multi=None, ladder=None, data_extra=None):
    """A room/enter body in the observed structure: stream_data and each sdk_params are JSON strings."""
    ladder = ladder or {"ld": _q("h264", "360x640", 500000), "hd": _q("h265", "720x1280", 1800000, v_rtbitrate=1500000,
                                                                       gop=4),
                        "origin": _q("h265", "1080x1920", 3000000, Fps=30), "ao": {"main": {"flv": "x"}}}
    data = {"id_str": "7000000000000000001", "id": 7000000000000000001, "status": 2,
            "stream_url": {"default_resolution": "HD", "resolution_name": {"HD": "720p"}, "stream_size_width": 720,
                           "stream_size_height": 1280,
                           "live_core_sdk_data": {"pull_data": {
                               "stream_data": json.dumps({"common": {"session_id": "s"}, "data": ladder}),
                               "options": {"default_quality": {"name": "hd"},
                                           "qualities": [{"name": "ld", "sdk_key": "ld", "level": 1, "resolution": "360x640",
                                                          "icon_type": 0},
                                                         {"name": "origin", "sdk_key": "origin", "level": 4,
                                                          "resolution": "1080x1920"}]}}}},
            "with_linkmic": False, "linkmic_layout": 0, "multi_stream_scene": 0, "linker_map": {},
            "link_mic": {"battle_settings": {"battle_status": 0, "battle_id": 0}, "audience_id_list": [],
                         "battle_scores": []}}
    data.update(multi or {})
    data.update(data_extra or {})
    return json.dumps({"data": data, "extra": {"now": 1}, "status_code": 0})


class Wire:
    """A fake CDP client: send() records the commands and returns their ids."""

    def __init__(self):
        self.sent = []

    def __call__(self, method, params, sid=None):
        self.sent.append((method, params, sid))
        return len(self.sent) + 100


def read_via_cdp(lst, wire, body_text, rid="r1", sid=None, b64=False, url=ENTER_URL, error=None):
    """responseReceived -> loadingFinished -> the getResponseBody reply, as Chrome sends them."""
    sess = {"sessionId": sid} if sid else {}
    n0 = len(wire.sent)
    lst.handle({"method": "Network.responseReceived", "params": {"requestId": rid, "response": {"url": url}}, **sess}, wire)
    lst.handle({"method": "Network.loadingFinished", "params": {"requestId": rid}, **sess}, wire)
    if len(wire.sent) == n0:
        return False                                                               # not matched: no body asked for
    cid = len(wire.sent) + 100
    assert wire.sent[-1] == ("Network.getResponseBody", {"requestId": rid}, sid)
    if error:
        lst.handle({"id": cid, "error": {"code": -32000, "message": error}}, wire)
    else:
        b = base64.b64encode(body_text.encode()).decode() if b64 else body_text
        lst.handle({"id": cid, "result": {"body": b, "base64Encoded": b64}}, wire)
    return True


def test_ladder_parsed():
    lst, w = B.TtRoomListener("ws://x"), Wire()
    assert read_via_cdp(lst, w, body(), b64=True)
    r = lst.tt_room_enter()
    assert r["n_reads"] == 1 and r["errors"] == 0 and len(r["reads"]) == 1 and r["overflow"] == 0
    rd = r["reads"][0]
    assert rd["path"] == "/webcast/room/enter/" and rd["from_worker"] is False and rd["room_id"] == "7000000000000000001"
    q = {x["name"]: x for x in rd["qualities"]}
    assert set(q) == {"ld", "hd", "origin", "ao"}
    assert q["ld"] == {"name": "ld", "VCodec": "h264", "resolution": "360x640", "vbitrate": 500000, "v_rtbitrate": None,
                       "fps": None, "gop": None}
    assert q["hd"]["VCodec"] == "h265" and q["hd"]["v_rtbitrate"] == 1500000 and q["hd"]["gop"] == 4
    assert q["origin"]["fps"] == 30                                               # "Fps" read as fps
    assert q["ao"]["VCodec"] is None and q["ao"]["vbitrate"] is None              # audio only: no sdk_params
    assert rd["options"] == [{"name": "ld", "sdk_key": "ld", "level": 1, "resolution": "360x640"},
                             {"name": "origin", "sdk_key": "origin", "level": 4, "resolution": "1080x1920"}]
    assert rd["stream"] == {"default_resolution": "HD", "resolution_name": {"HD": "720p"}, "stream_size_width": 720,
                            "stream_size_height": 1280}
    flat = json.dumps(r)
    assert "SECRET" not in flat and "pull-flv" not in flat and "AudioCodec" not in flat   # no URL, no body
    assert r["rule"] and rd["status_code"] == 0 and rd["parse_error"] is None and rd["body_bytes"] > 0


def test_multi_guest_flagged_and_plain_not():
    lst, w = B.TtRoomListener("ws://x"), Wire()
    read_via_cdp(lst, w, body())
    plain = lst.reads[-1]
    assert plain["multi_guest"] is False and plain["fired"] == []
    assert plain["multi_guest_signals"] == {"with_linkmic": False, "linkmic_layout": 0, "multi_stream_scene": 0,
                                            "linker_map_size": 0, "battle_status": 0, "battle_id": 0,
                                            "audience_id_list_n": 0, "battle_scores_n": 0}
    battle = {"with_linkmic": True, "linkmic_layout": 2, "linker_map": {"a": {}, "b": {}},
              "link_mic": {"battle_settings": {"battle_status": 1, "battle_id": 99}, "audience_id_list": [1, 2, 3],
                           "battle_scores": [{"s": 1}, {"s": 2}]}}
    read_via_cdp(lst, w, body(multi=battle), rid="r2")
    mg = lst.reads[-1]
    assert mg["multi_guest"] is True and mg["fired"] == ["with_linkmic", "linkmic_layout", "linker_map", "battle_status"]
    s = mg["multi_guest_signals"]
    assert s["linker_map_size"] == 2 and s["battle_id"] == 99 and s["audience_id_list_n"] == 3 and s["battle_scores_n"] == 2
    one = B.TtRoomListener.parse(body(multi={"multi_stream_scene": 3}))      # a single condition is enough
    assert one["multi_guest"] is True and one["fired"] == ["multi_stream_scene"]
    # missing keys -> None, never False
    bare = json.loads(body())
    for k in ("with_linkmic", "linkmic_layout", "multi_stream_scene", "linker_map", "link_mic"):
        bare["data"].pop(k)
    p = B.TtRoomListener.parse(json.dumps(bare))
    assert p["multi_guest"] is None and p["fired"] == []
    assert all(v is None for v in p["multi_guest_signals"].values())


def test_unchanged_reread_not_recorded():
    lst, w = B.TtRoomListener("ws://x"), Wire()
    read_via_cdp(lst, w, body(), rid="a")
    read_via_cdp(lst, w, body(), rid="b")                                         # a re-entry, same room and ladder
    read_via_cdp(lst, w, body(ladder={"ld": _q("h264", "360x640", 600000)}), rid="c")
    read_via_cdp(lst, w, body(ladder={"ld": _q("h264", "360x640", 600000)}), rid="d")
    r = lst.tt_room_enter()
    assert r["n_reads"] == 4 and len(r["reads"]) == 2 and r["reads"][1]["qualities"][0]["vbitrate"] == 600000
    saved = B.WATCH_LOG_MAX
    try:
        B.WATCH_LOG_MAX = 2
        read_via_cdp(lst, w, body(multi={"with_linkmic": True}), rid="e")
        assert lst.tt_room_enter()["overflow"] == 1 and len(lst.reads) == 2       # capped, counted
    finally:
        B.WATCH_LOG_MAX = saved


def test_worker_session_read():
    lst, w = B.TtRoomListener("ws://x"), Wire()
    lst.handle({"method": "Target.attachedToTarget",
                "params": {"sessionId": "W1", "targetInfo": {"type": "worker"}}}, w)
    assert w.sent[-1] == ("Network.enable", B.TtRoomListener.NETWORK, "W1")
    lst.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "F1", "targetInfo": {"type": "iframe"}}}, w)
    assert len(w.sent) == 1                                                        # only workers
    assert read_via_cdp(lst, w, body(), rid="r9", sid="W1")
    rd = lst.tt_room_enter()["reads"][0]
    assert rd["from_worker"] is True and lst.tt_room_enter()["workers"] == 1
    # another path is matched (room/info) and keyed apart; other webcast calls are not read
    assert read_via_cdp(lst, w, body(), rid="i1", url="https://webcast.tiktok.com/webcast/room/info/?room_id=7")
    assert lst.reads[-1]["path"] == "/webcast/room/info/"
    assert not read_via_cdp(lst, w, body(), rid="x1", url="https://webcast.tiktok.com/webcast/im/fetch/?room_id=7")
    assert not read_via_cdp(lst, w, body(), rid="x2", url="https://www.tiktok.com/@a/live?x=/webcast/room/enter/")
    lst.handle({"method": "Network.responseReceived", "params": {"requestId": "f", "response": {"url": ENTER_URL}}}, w)
    lst.handle({"method": "Network.loadingFailed", "params": {"requestId": "f"}}, w)
    assert ("None", "f") not in lst._wanted and (None, "f") not in lst._wanted


def test_malformed_body_counted():
    lst, w = B.TtRoomListener("ws://x"), Wire()
    read_via_cdp(lst, w, "<html>not json", rid="m1")
    read_via_cdp(lst, w, "[1, 2]", rid="m2")
    read_via_cdp(lst, w, "", rid="m3", error="No resource with given identifier found")
    r = lst.tt_room_enter()
    assert r["errors"] == 3 and r["reads"] == [] and "getResponseBody" in r["last_error"]
    bad = json.loads(body())                                                       # stream_data that does not parse
    bad["data"]["stream_url"]["live_core_sdk_data"]["pull_data"]["stream_data"] = "{not json"
    read_via_cdp(lst, w, json.dumps(bad), rid="m4")
    r = lst.tt_room_enter()
    assert r["errors"] == 4 and len(r["reads"]) == 1 and "stream_data" in r["last_error"]
    rd = r["reads"][0]
    assert rd["qualities"] is None and rd["multi_guest"] is False and rd["parse_error"].startswith("stream_data")
    read_via_cdp(lst, w, body(), rid="m5")                                         # the listener goes on
    assert lst.tt_room_enter()["reads"][-1]["qualities"] is not None


def test_wiring():
    src = (HERE / "browser.py").read_text()
    i_tt = src.index("self._tt_room = TtRoomListener(self._ws_url).start()")
    # before the room navigation: levi 07:48, the listener starts in _reset_for_session, after the unshaped discovery
    # (its own tab) and before _join_nav opens the room on the session page
    assert i_tt < src.index("    def _join_nav(self)") < src.index("self._tt_try_rooms(*self._tt_discovered)")
    assert src.index("    def _reset_for_session(self)") < i_tt
    assert src.index("self._media = MediaListener(") < i_tt
    i_stop = src.index("            self._master.stop()\n")
    assert "self._tt_room.stop()" in src[i_stop:i_stop + 120]
    assert "tt_room_enter=(browser._tt_room.tt_room_enter()" in (HERE / "exp.py").read_text()


def test_master_listener_untouched():
    """twitch's MasterListener keeps its own matching (usher master + media playlists); a tt room/enter is not its."""
    ml = B.MasterListener("ws://x")
    w = Wire()
    ml.handle({"method": "Network.responseReceived", "params": {"requestId": "r", "response": {"url": ENTER_URL}}}, w)
    assert ml._wanted == {}
    assert not hasattr(ml, "tt_room_enter") and B.MasterListener.WORKER_TYPES == B.TtRoomListener.WORKER_TYPES


def main() -> int:
    test_ladder_parsed()
    test_multi_guest_flagged_and_plain_not()
    test_unchanged_reread_not_recorded()
    test_worker_session_read()
    test_malformed_body_counted()
    test_wiring()
    test_master_listener_untouched()
    print("test_tt_room_enter: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
