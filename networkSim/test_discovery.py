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
CHROME_EXPECT = 'Path(__file__).resolve().parent.parent / "current_data" / f"chrome{\'_\' + slot.tag if slot is not None else \'\'}.log"'


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


def _session(platform, monkeypatch, first_url=None, disc_evals=None, closes=True):
    d = Path(tempfile.mkdtemp()) / "_trio" / platform / f"{platform[:2]}_100001"
    d.mkdir(parents=True)
    log: list = []
    b = B.BrowserSession(platform, session_dir=d, first_url=first_url)
    b._prepared = True
    b._page = _Page(log, "session")
    monkeypatch.setattr(B, "_discovery_tab", lambda: (("ctx", "tid"), _Page(log, "disc", disc_evals)))

    def close(ids, page):                     # the verified close (brad / levi 08:18 (i)): its outcome is the test's
        page.close()
        log.append(("dispose", ids))
        return closes
    monkeypatch.setattr(B, "_close_discovery_tab", close)
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
    assert 'JOIN["discovery"] = {**browser.discovery, "tc_at_start": _tc0}' in main


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
    assert rec["tab_closed"] is True
    assert ("publish", "twitch", "https://www.twitch.tv/abc", "intent") in log
    assert b._tw_discovered["pick"] == "https://www.twitch.tv/abc"
    b2, _ = _session("twitch", monkeypatch, first_url=TW_DIR,
                     disc_evals={"preview-card-image-link": json.dumps({"pick": None, "candidates": []})})
    with pytest.raises(RuntimeError, match=r"Failed to navigate to a live twitch stream \(discovery"):
        b2.discover()


def test_a_tab_that_does_not_close_raises_before_any_shaping(monkeypatch):
    b, log = _session("tiktok", monkeypatch, closes=False)
    monkeypatch.setattr(b, "_tt_pool", lambda page, sc: ([{"href": "https://www.tiktok.com/@a/live", "text": "LIVE"}], 0))
    with pytest.raises(RuntimeError, match=r"discovery: its tab did not close"):
        b.discover()
    assert b.discovery["tab_closed"] is False and ("reset",) not in log
    b2, _ = _session("tiktok", monkeypatch)
    monkeypatch.setattr(b2, "_tt_pool", lambda page, sc: ([{"href": "https://www.tiktok.com/@a/live", "text": "LIVE"}], 0))
    assert b2.discover()["tab_closed"] is True


def test_the_discovery_tab_lives_in_its_own_browser_context(monkeypatch):
    """brad / levi 08:18 (iii): createBrowserContext -> createTarget(browserContextId) -> disposeBrowserContext, then the
    target is checked gone from /json."""
    calls, targets = [], [{"id": "T1", "webSocketDebuggerUrl": "ws://x/devtools/page/T1"}]

    def bc(method, params=None):
        calls.append((method, params))
        if method == "Target.createBrowserContext":
            return {"browserContextId": "C1"}
        if method == "Target.createTarget":
            return {"targetId": "T1"}
        if method == "Target.disposeBrowserContext":
            targets.clear()
        return {}
    monkeypatch.setattr(B, "_browser_call", bc)
    monkeypatch.setattr(B, "_cdp_targets", lambda: list(targets))
    monkeypatch.setattr(B, "CDPPage", lambda ws: _Page([], ws))
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    ids, page = B._discovery_tab()
    assert ids == ("C1", "T1") and calls[1] == ("Target.createTarget", {"url": "about:blank", "browserContextId": "C1"})
    assert B._close_discovery_tab(ids, page) is True
    assert ("Target.disposeBrowserContext", {"browserContextId": "C1"}) in calls
    targets.append({"id": "T1"})                                       # a target that never goes away
    monkeypatch.setattr(B, "_browser_call", lambda m, p=None: {})
    monkeypatch.setattr(B, "DISCOVERY_CLOSE_WAIT_S", 0.05)
    assert B._close_discovery_tab(ids, page) is False


def test_twitch_join_raises_on_a_held_or_missing_discovered_pick(monkeypatch):
    """brad / levi 08:18 (2): never the LIVE_URLS directory walk (a tile click) on the shaped link."""
    pick = "https://www.twitch.tv/abc"

    class _L:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return self
    monkeypatch.setattr(B, "MasterListener", _L)
    walked = []
    monkeypatch.setattr(B, "_navigate_to_live", lambda *a, **k: walked.append(k.get("first_url")) or None)
    for held, discovered in ((True, {"pick": pick}), (False, {"pick": None})):
        b, _ = _session("twitch", monkeypatch, first_url=TW_DIR)
        b._tw_discovered, b._ws_url = discovered, "ws://x/devtools/page/S"      # _prepare sets the page's ws url
        monkeypatch.setattr(B, "sibling_streams", lambda sd, plat, _h=held: [pick] if _h else [])
        with pytest.raises(RuntimeError, match="no directory walk on the shaped link"):
            b._join_nav()
    assert walked == []


def test_the_chrome_log_copy_matches_the_makefile():
    """brad's note: copy_chrome_log rebuilds the Makefile's CHROME_LOG; pinned so a rename cannot copy nothing."""
    mk = (HERE / "Makefile").read_text()
    assert "CAPTURE_DIR := $(realpath $(MAKEFILE_DIR)/../current_data)" in mk
    assert "CHROME_LOG := $(CAPTURE_DIR)/chrome$(SLOT_SUFFIX).log" in mk
    assert 'f"SLOT_SUFFIX=_{s.tag}"' in (HERE / "trio_slots.py").read_text()
    assert CHROME_EXPECT in (HERE / "exp.py").read_text()


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
        assert CS.join_order_violation(CS.join_order(sd, meta, net)).startswith("join_order: 1 media request")
        assert CS.join_order(sd, {"join": {}}, net) is None                                 # before the rule
        assert CS.join_order_violation(None) is None


def test_media_before_the_shaped_join_fails_the_session(tmp_path):
    """levi 08:15: a simulated session whose stream media starts after the discovery and before the first apply FAILs
    in check_session (its check.json reasons), and the same session with the order held does not carry that reason."""
    import subprocess
    t0 = 1_800_000_000.0
    sd = tmp_path / "data" / "_trio" / "tiktok" / "ti_100001"
    sd.mkdir(parents=True)
    (sd / "meta.json").write_text(json.dumps({"platform": "tiktok", "ended_at": t0 + 600, "started_at": t0,
                                              "join": {"discovery": {"kind": "tt /live", "t_end": t0 + 20.0}}}))
    (sd / "net_labels.txt").write_text(json.dumps({"timestamp": t0 + 23.0, "apply_start": t0 + 22.9, "state": 3,
                                                   "speed": 600}) + "\n")
    (sd / "labels.csv").write_text("ts,type\n")

    def run(leak):
        recs = [{"request_url": "https://x/stream-2_or4.flv", "request_start": t0 + 30.0}]
        if leak:
            recs.append({"request_url": "https://x/stream-2_or4.flv", "request_start": t0 + 21.0})
        (sd / "http_v2.json").write_text(json.dumps({"a->b": recs}))
        subprocess.run([sys.executable, str(HERE / "check_session.py"), str(sd)], capture_output=True, text=True)
        return json.loads((sd / "check.json").read_text())
    c = run(True)
    assert c["verdict"] == "FAIL" and any(r.startswith("join_order:") for r in c["reasons"]), c["reasons"]
    assert c["join_order"]["media_between_discovery_and_apply"] == 1
    c = run(False)
    assert not any(r.startswith("join_order:") for r in c["reasons"]) and c["join_order"]["media_between_discovery_and_apply"] == 0
    # brad / levi 08:18: room media after the apply on a flow that carried a request during the discovery = FAIL
    (sd / "http_v2.json").write_text(json.dumps({
        "a->b": [{"request_url": "https://x/stream-9_ld5.flv", "request_start": t0 + 5.0},           # a preview
                 {"request_url": "https://x/stream-2_or4.flv", "request_start": t0 + 30.0}]}))      # the room, reused
    subprocess.run([sys.executable, str(HERE / "check_session.py"), str(sd)], capture_output=True, text=True)
    c = json.loads((sd / "check.json").read_text())
    assert c["verdict"] == "FAIL" and c["join_order"]["room_media_on_discovery_flow"] == 1
    assert any("on a flow opened during the unshaped discovery" in r for r in c["reasons"]), c["reasons"]


def main() -> int:
    return pytest.main(["-q", __file__])                 # it uses monkeypatch: pytest also in script mode


if __name__ == "__main__":
    sys.exit(main())
