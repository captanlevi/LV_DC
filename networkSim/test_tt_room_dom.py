#!/usr/bin/env python3
"""ops/diag/tt_room_dom.py (supervisor 2026-10-19, ti_100010): the trio / N-slot tt sessions the slots log shows running
are sampled on their own slot's Chrome (meta.json concurrency.cdp_port), each record carrying the port; finished,
other-platform and superseded sessions are not; single mode still reads the batch Chrome's base. No browser: CDP is
stubbed.

    python3 test_tt_room_dom.py        (exit 0 = pass)
"""
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

try:
    import websocket  # noqa: F401
except ImportError:                               # no browser needed: CDP is stubbed below
    sys.modules["websocket"] = types.ModuleType("websocket")
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tt_room_dom", HERE.parent / "ops" / "diag" / "tt_room_dom.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def _sess(troot: Path, name: str, port: int, finished: bool = False, platform: str = "tiktok") -> Path:
    sd = troot / platform / name
    sd.mkdir(parents=True)
    (sd / "meta.json").write_text(json.dumps({"concurrency": {"cdp_port": port, "tag": "tt1"}}))
    if finished:
        (sd / "check.md").write_text("done")
    return sd


def test_running_trio_tt_sessions_and_their_ports():
    with tempfile.TemporaryDirectory() as d:
        troot = Path(d) / "_trio"
        _sess(troot, "ti_100001", 9225)                                  # superseded on its slot
        live = _sess(troot, "ti_100002", 9225)
        live2 = _sess(troot, "ti_100003", 9227)                          # N = 4: a second tt slot
        _sess(troot, "ti_100004", 9228, finished=True)                   # ended without an end event: check.md
        _sess(troot, "tw_100005", 9224, platform="twitch")
        ev = [{"event": "start", "slot": "tt1", "session": "ti_100001", "platform": "tiktok"},
              {"event": "start", "slot": "tt1", "session": "ti_100002", "platform": "tiktok"},
              {"event": "start", "slot": "tt2", "session": "ti_100003", "platform": "tiktok"},
              {"event": "start", "slot": "tt3", "session": "ti_100004", "platform": "tiktok"},
              {"event": "start", "slot": "tw1", "session": "tw_100005", "platform": "twitch"},
              {"event": "start", "slot": "tt4", "session": "ti_100006", "platform": "tiktok"},
              {"event": "end", "slot": "tt4", "session": "ti_100006"}]
        (troot / "_summaries").mkdir(parents=True)
        (troot / R.T.SLOTS_LOG).write_text("".join(json.dumps(e) + "\n" for e in ev) + "not json\n")
        got = R.running_trio_tt(troot)
        assert sorted((s.name, p) for s, p in got) == [("ti_100002", 9225), ("ti_100003", 9227)], got
        bases = []

        class Page:
            def __init__(self, ws):
                bases.append(R.B.CDP_BASE)

            def eval(self, js):
                return json.dumps({"url": "https://www.tiktok.com/@x/live", "videos": [[537, 676, 4]]})

            def close(self):
                pass
        saved = (R.B.CDPPage, R.B._get_page_ws_url, R.B.CDP_BASE)
        R.B.CDPPage, R.B._get_page_ws_url = Page, lambda: "ws://stub"
        try:
            for sd, port in got:
                R.sample(sd, f"http://localhost:{port}", port)
        finally:
            R.B.CDPPage, R.B._get_page_ws_url, R.B.CDP_BASE = saved
        assert bases == ["http://localhost:9225", "http://localhost:9227"], bases
        rec = json.loads((live / "tt_room_dom.jsonl").read_text())
        assert rec["cdp_port"] == 9225 and rec["videos"] == [[537, 676, 4]] and "ts" in rec
        assert json.loads((live2 / "tt_room_dom.jsonl").read_text())["cdp_port"] == 9227
        assert R.running_trio_tt(Path(d) / "nothing") == []
    assert R.SINGLE_BASE == R.B.CDP_BASE                                  # single mode: the batch Chrome's base


def main() -> int:
    test_running_trio_tt_sessions_and_their_ports()
    test_room_enter_fields_and_names()
    test_net_listener_events()
    test_net_listener_failure_never_raises()
    print("test_tt_room_dom: ok")
    return 0


def test_room_enter_fields_and_names():
    """bundle item 1: the fixed room/enter fields (a reduced real response), and the message-type name counts."""
    body = json.loads((HERE / "fixtures" / "tt_room_enter_reduced.json").read_text())
    f = R.room_enter_fields(body)
    assert f == {"room_id": "7600000000000000001", "battle_status": 0, "battle_id": "0", "n_linked_users": 0,
                 "n_cohost_linked": 0, "multi_guest_enable": False, "linkmic_layout": 0, "room_layout": 0,
                 "live_type_linkmic": False, "pk_status": "-1"}, f
    battle = {"data": {"link_mic": {"battle_settings": {"battle_status": 1, "battle_id": 99}, "linked_user_list": [{}, {}]},
                       "social_interaction": {"cohost": {"linked_users": [{}]}}}}
    g = R.room_enter_fields(battle)
    assert g["battle_status"] == 1 and g["battle_id"] == "99" and g["n_linked_users"] == 2 and g["n_cohost_linked"] == 1
    assert g["room_id"] is None
    assert R.room_enter_fields({}) ["n_linked_users"] == 0
    pb = b"\x0a\x14WebcastLinkMicBattle\x12\x00\x0a\x14WebcastLinkMicBattle\x0a\x17WebcastLinkLayerMessage"
    assert R.count_names(pb) == {"WebcastLinkMicBattle": 2, "WebcastLinkLayerMessage": 1}
    assert R.count_names(b"WebcastChatMessage hello") == {}                       # chat is never counted or kept


class _WS:
    def __init__(self):
        self.sent = []

    def send(self, s):
        self.sent.append(json.loads(s))


def test_net_listener_events():
    """The listener remembers matched responses, fetches each body once loaded, writes one room_enter row and adds the
    im/fetch and websocket name counts to the next 30 s row; other traffic is ignored; nothing else is stored."""
    import base64
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d) / "ti_1"
        sd.mkdir()
        L = R.NetListener(sd, "ws://x", 9226)
        ws, bodies = _WS(), {}
        ev = lambda m, **p: {"method": m, "params": p}
        L.handle(ws, ev("Network.responseReceived", requestId="1",
                        response={"url": "https://webcast.tiktok.com/webcast/room/enter/?aid=1988"}), bodies)
        L.handle(ws, ev("Network.responseReceived", requestId="2",
                        response={"url": "https://webcast.tiktok.com/webcast/im/fetch/?x=1"}), bodies)
        L.handle(ws, ev("Network.responseReceived", requestId="3",
                        response={"url": "https://pull-flv.tiktokcdn.com/stage/stream.flv"}), bodies)   # media: ignored
        L.handle(ws, ev("Network.loadingFinished", requestId="3"), bodies)
        assert ws.sent == []
        L.handle(ws, ev("Network.loadingFinished", requestId="1"), bodies)
        L.handle(ws, ev("Network.loadingFinished", requestId="2"), bodies)
        assert [m["method"] for m in ws.sent] == ["Network.getResponseBody"] * 2
        enter = json.dumps(json.loads((HERE / "fixtures" / "tt_room_enter_reduced.json").read_text()))
        L.handle(ws, {"id": ws.sent[0]["id"], "result": {"body": enter, "base64Encoded": False}}, bodies)
        im = base64.b64encode(b"..WebcastLinkMicArmies..WebcastLinkMicArmies..").decode()
        L.handle(ws, {"id": ws.sent[1]["id"], "result": {"body": im, "base64Encoded": True}}, bodies)
        frame = base64.b64encode(b"..WebcastLinkMicMethod..").decode()
        L.handle(ws, ev("Network.webSocketFrameReceived", response={"opcode": 2, "payloadData": frame}), bodies)
        rows = [json.loads(l) for l in (sd / "tt_room_dom.jsonl").read_text().splitlines()]
        assert len(rows) == 1 and rows[0]["kind"] == "room_enter" and rows[0]["battle_status"] == 0
        assert set(rows[0]) == {"kind", "ts", "cdp_port", "room_id", "battle_status", "battle_id", "n_linked_users",
                                "n_cohost_linked", "multi_guest_enable", "linkmic_layout", "room_layout",
                                "live_type_linkmic", "pk_status"}                    # the fixed fields only
        assert L.take_counts() == {"WebcastLinkMicArmies": 2, "WebcastLinkMicMethod": 1} and L.take_counts() == {}
        assert bodies == {} and L.pending == {}


def test_net_listener_failure_never_raises():
    with tempfile.TemporaryDirectory() as d:
        def boom(url):
            raise OSError("no page")
        L = R.NetListener(Path(d), "ws://x", None, connect=boom)
        L.run()                                                         # logs and returns
        assert L.errors == 1
    assert R.NET_BUFFER["maxResourceBufferSize"] <= 1024 * 1024        # bounded buffers (supervisor 03:54)


if __name__ == "__main__":
    sys.exit(main())
