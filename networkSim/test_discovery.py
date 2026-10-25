#!/usr/bin/env python3
"""levi 07:48 (supervisor 07:48; WC3b r1 ti_100001: /live loaded on the shaped link, 0 candidates): discovery runs
UNSHAPED before the shaper's first apply, in its own tab closed before it returns (i); the stream is opened by its direct
URL on the session page (ii); the extension storage is cleared after the tab closed; an empty list is re-navigated once,
shot and raises a startup failure before any shaping; check_session reports the order (iii)."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    import types
    _ws = types.ModuleType("websocket")             # the stub test_drain expects (never a bare module)
    _ws.WebSocketTimeoutException = type("WebSocketTimeoutException", (Exception,), {})
    _ws.WebSocket = object
    sys.modules["websocket"] = _ws
import browser as B  # noqa: E402
import check_session as CS  # noqa: E402

TW_DIR = "https://www.twitch.tv/directory/category/just-chatting"


class _Page:
    def __init__(self, log, name, evals=None):
        self.log, self.name, self.evals = log, name, evals or {}

    def navigate(self, url):
        self.log.append((self.name, "nav", url))

    def eval(self, js, await_promise=False):
        self.log.append((self.name, "eval"))
        for key, val in self.evals.items():
            if key in js:
                return val(js) if callable(val) else val
        return None

    def call(self, method, params=None):
        self.log.append((self.name, "call", method))
        return {"data": ""}

    def close(self):
        self.log.append((self.name, "close"))


def _session(platform, monkeypatch, first_url=None, disc_evals=None):
    d = Path(tempfile.mkdtemp()) / "_trio" / platform / f"{platform[:2]}_100001"
    d.mkdir(parents=True)
    log: list = []
    b = B.BrowserSession(platform, session_dir=d, first_url=first_url)
    b._prepared = True
    b._page = _Page(log, "session")
    monkeypatch.setattr(B, "_discovery_tab", lambda: ("tid", _Page(log, "disc", disc_evals)))
    monkeypatch.setattr(B, "_close_target", lambda tid: log.append(("close_target", tid)))
    monkeypatch.setattr(B, "_wait_for_load", lambda page, timeout=15: None)
    monkeypatch.setattr(B, "publish_stream", lambda *a, **k: log.append(("publish",) + a[1:]))
    monkeypatch.setattr(b, "_reset_for_session", lambda: log.append(("reset",)))
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    return b, log


def _idx(log, pred):
    return next(i for i, x in enumerate(log) if pred(x))


def test_exp_discovers_before_the_join_thread_and_the_shaper():
    src = (HERE / "exp.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    assert main.index("browser.discover()") < main.index("_join_t.start()") < main.index("asyncio.run(experiment(")
    assert 'JOIN["discovery"] = dict(browser.discovery)' in main


def test_tt_discovers_in_its_own_tab_then_opens_the_room_by_url(monkeypatch):
    cands = [{"href": "https://www.tiktok.com/@a/live", "text": "LIVE 1.2K viewers"},
             {"href": "https://www.tiktok.com/@b/live", "text": "LIVE 300 viewers"}]
    b, log = _session("tiktok", monkeypatch)
    pools = []
    monkeypatch.setattr(b, "_tt_pool", lambda page, sc: (pools.append(page.name), (list(cands), 0))[1])
    rec = b.discover()
    assert pools == ["disc"] and rec["kind"] == "tt /live" and rec["pool_size"] == 2 and rec["t_end"] >= rec["t_start"]
    assert _idx(log, lambda x: x == ("disc", "close")) < _idx(log, lambda x: x == ("reset",))     # (i), then the clear
    assert not [x for x in log if x[0] == "session"]                  # the session page never loaded the list
    room, order = b._tt_discovered
    assert room["discovery"].startswith("unshaped") and order[0]["href"].endswith("/@a/live")
    monkeypatch.setattr(B, "_tt_confirm_playing", lambda page, s, scale=1.0: (True, {}))
    monkeypatch.setattr(B, "identity_check", lambda *a: {"ok": True})
    monkeypatch.setattr(B, "_tt_preroll", lambda *a, **k: (True, {"ct_advance_s": 30.0}))
    assert b._tt_try_rooms(room, order) == B.ROOM_ROTATION_LIVE_URL
    navs = [x[2] for x in log if x[:2] == ("session", "nav")]
    assert navs == ["https://www.tiktok.com/@a/live"]                 # (ii): its direct URL, no /live, no tile


def test_an_empty_tt_list_is_renavigated_shot_and_raises_before_any_shaping(monkeypatch):
    b, log = _session("tiktok", monkeypatch, disc_evals={"performance.getEntriesByType": json.dumps(
        {"resources": 40, "status_0": 12, "status_ge_400": 1})})
    calls = []
    monkeypatch.setattr(b, "_tt_pool", lambda page, sc: (calls.append(1), ([], 1))[1])
    with pytest.raises(RuntimeError, match=r"Failed to navigate to a live tiktok stream \(discovery"):
        b.discover()
    rec = b.tt_room_choice
    assert len(calls) == 2 and rec["renavigated"] and rec["page_resources"]["status_0"] == 12
    assert ("disc", "call", "Page.captureScreenshot") in log and ("disc", "close") in log    # shot; the tab closed
    assert b.discovery["kind"] == "tt /live" and "t_end" in b.discovery
    assert ("reset",) not in log                                      # never reached the session's storage clear


def test_twitch_directory_is_read_not_clicked_and_opened_by_url(monkeypatch):
    seen = {}

    def pick(js):
        seen["js"] = js
        return json.dumps({"pick": "https://www.twitch.tv/abc", "candidates": ["https://www.twitch.tv/abc"]})
    b, log = _session("twitch", monkeypatch, first_url=TW_DIR,
                      disc_evals={"preview-card-image-link": pick, "a[data-a-target": True})
    rec = b.discover()
    assert rec["kind"] == "twitch directory" and rec["pick"] == "https://www.twitch.tv/abc"
    assert seen["js"].rstrip().endswith('false)') and ("disc", "nav", TW_DIR) in log         # click=False
    assert ("disc", "close") in log and _idx(log, lambda x: x == ("disc", "close")) < _idx(log, lambda x: x == ("reset",))
    assert ("publish", "twitch", "https://www.twitch.tv/abc", "intent") in log
    assert b._tw_discovered["pick"] == "https://www.twitch.tv/abc"
    b2, _ = _session("twitch", monkeypatch, first_url=TW_DIR,
                     disc_evals={"preview-card-image-link": json.dumps({"pick": None, "candidates": []})})
    with pytest.raises(RuntimeError, match=r"Failed to navigate to a live twitch stream \(discovery"):
        b2.discover()


def test_a_direct_url_needs_no_discovery(monkeypatch):
    b, log = _session("youtube", monkeypatch, first_url="https://www.youtube.com/@x/live")
    assert b.discover()["kind"].startswith("none") and ("reset",) in log
    assert not [x for x in log if x[0] == "disc"]


def test_check_session_reports_the_join_order():
    t0 = 1_800_000_000.0
    meta = {"join": {"discovery": {"kind": "tt /live", "t_start": t0, "t_end": t0 + 20.0}}}
    net = [{"timestamp": t0 + 23.0, "apply_start": t0 + 22.9, "state": 3}]
    recs = [{"request_url": "https://x/stream-1_ld5.flv", "request_start": t0 + 5.0},            # a preview
            {"request_url": "https://x/stream-2_or4.flv", "request_start": t0 + 30.0},           # the room
            {"request_url": "https://x/page.js", "request_start": t0 + 21.0}]
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        (sd / "http_v2.json").write_text(json.dumps({"a->b": recs}))
        jo = CS.join_order(sd, meta, net)
        assert jo["discovery_media_requests"] == 1 and jo["media_between_discovery_and_apply"] == 0
        assert abs(jo["room_first_media_after_apply_s"] - 7.1) < 1e-6
        (sd / "http_v2.json").write_text(json.dumps({"a->b": recs + [
            {"request_url": "https://x/stream-1_ld5.flv", "request_start": t0 + 21.5}]}))     # a leak: preview after
        assert CS.join_order(sd, meta, net)["media_between_discovery_and_apply"] == 1
        assert CS.join_order(sd, {"join": {}}, net) is None                                 # before the rule


def main() -> int:
    return pytest.main(["-q", __file__])                 # it uses monkeypatch: pytest also in script mode


if __name__ == "__main__":
    sys.exit(main())
