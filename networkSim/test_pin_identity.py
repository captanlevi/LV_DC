#!/usr/bin/env python3
"""WA2 round 1 (00:07): a pinned tt room page (/@user/live) ran the tt pick, which clicked the side nav's first suggested
room, and nothing compared the landed room with the intended one. A room page is never picked from; the landed tt room /
twitch channel / yt owner is checked against the URL navigated (pin_mismatch, a named startup failure); an unpinned tt
try that lands elsewhere fails over to the next candidate (room_mismatch); an unreadable identity is recorded, never an
abort.

    python3 test_pin_identity.py        (exit 0 = pass)
"""
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

ROOM = "https://www.tiktok.com/@stef_gamingg/live"


class Page:
    """A room page with the side nav's suggested rooms: the tt pick, if evaluated, clicks the first one."""
    def __init__(self, href=None, owner=None):
        self.href, self.owner, self.evals = href, owner, []

    def navigate(self, url):
        self.href = url

    def eval(self, js, await_promise=False):
        self.evals.append(js)
        if js == "location.href":
            return self.href
        if js == "document.readyState":
            return "complete"
        if js == B._YT_OWNER_JS:
            return self.owner
        if js == B._PICK_STREAM_JS["tiktok"]:
            self.href = "https://www.tiktok.com/@coolgaming200/live"     # the side nav's first suggested room
            return self.href
        if js == B._CARD_READY_JS["tiktok"]:
            return True
        return None


def _nav(page, url):
    saved = (B.time.sleep, B._video_is_advancing)
    try:
        B.time.sleep, B._video_is_advancing = (lambda s: None), (lambda p: True)
        return B._navigate_to_live(page, "tiktok", first_url=url, timeout=1)
    finally:
        B.time.sleep, B._video_is_advancing = saved


def test_a_room_page_is_never_picked_from():
    p = Page()
    assert _nav(p, ROOM) == ROOM
    assert B._PICK_STREAM_JS["tiktok"] not in p.evals and p.href == ROOM
    q = Page()                                           # the test has power: the explore page still picks
    _nav(q, "https://www.tiktok.com/live")
    assert B._PICK_STREAM_JS["tiktok"] in q.evals and q.href.endswith("@coolgaming200/live")


def test_page_identity():
    assert B.page_identity("tiktok", ROOM) == "@stef_gamingg"
    assert B.page_identity("tiktok", "https://www.tiktok.com/@Stef_Gamingg/live?enter_from=x") == "@stef_gamingg"
    assert B.page_identity("tiktok", "https://www.tiktok.com/live") is None
    assert B.page_identity("twitch", "https://www.twitch.tv/ESLCS") == "eslcs"
    assert B.page_identity("twitch", "https://www.twitch.tv/directory/category/chess") is None
    assert B.page_identity("youtube", "https://www.youtube.com/@AlJazeeraEnglish/live") == "@aljazeeraenglish"
    assert B.page_identity("youtube", "http://www.youtube.com/@aljazeeraenglish") == "@aljazeeraenglish"
    assert B.page_identity("youtube", "https://www.youtube.com/watch?v=abc") is None


def test_identity_check_names_a_mismatch_and_never_aborts_unverifiable():
    r = B.identity_check("tiktok", ROOM, Page("https://www.tiktok.com/@coolgaming200/live"))
    assert r["ok"] is False and r["intended"] == "@stef_gamingg" and r["landed"] == "@coolgaming200"
    assert B.identity_check("tiktok", ROOM, Page(ROOM))["ok"] is True
    yt = "https://www.youtube.com/@AlJazeeraEnglish/live"
    assert B.identity_check("youtube", yt, Page("https://www.youtube.com/watch?v=x",
                                                owner="http://www.youtube.com/@AlJazeeraEnglish"))["ok"] is True
    assert B.identity_check("youtube", yt, Page("https://www.youtube.com/watch?v=x",
                                                owner="http://www.youtube.com/@other"))["ok"] is False
    for owner in (None, "http://www.youtube.com/channel/UC123"):        # unreadable / legacy: recorded, never an abort
        assert B.identity_check("youtube", yt, Page("https://www.youtube.com/watch?v=x", owner=owner))["ok"] is None
    assert B.identity_check("twitch", "https://www.twitch.tv/directory/category/x", Page("https://www.twitch.tv/a"))["ok"] is None


def _session(platform, page, first_url=None, channel_url=None):
    s = B.BrowserSession.__new__(B.BrowserSession)
    s.platform, s._page, s.pin_check, s.abort_reason, s.abort_at = platform, page, None, None, None
    s.first_url, s.channel_url = first_url, channel_url
    return s


def _raises(fn, *a):
    try:
        fn(*a)
    except RuntimeError as e:
        return str(e)
    raise AssertionError("must raise")


def test_a_landed_mismatch_is_a_named_startup_failure():
    import os
    saved = os.environ.pop("LVDC_PICK_PINNED", None)
    try:
        s = _session("tiktok", Page("https://www.tiktok.com/@coolgaming200/live"))
        assert _raises(s._check_landed, ROOM).startswith("room_mismatch: wanted @stef_gamingg, landed @coolgaming200")
        assert s.pin_check["ok"] is False                # recorded for meta.json pin_check
        os.environ["LVDC_PICK_PINNED"] = '{"room": "@stef_gamingg"}'
        assert _raises(s._check_landed, ROOM).startswith("pin_mismatch: wanted @stef_gamingg")
        # levi 00:38 S2: a PINNED session whose identity cannot be read is refused, never passed
        yt = _session("youtube", Page("https://www.youtube.com/watch?v=x", owner=None))
        assert _raises(yt._check_landed, "https://www.youtube.com/@AlJazeeraEnglish/live").startswith("pin_mismatch: unverifiable")
        s._page = Page(ROOM)
        s._check_landed(ROOM)
        assert s.pin_check["ok"] is True
        os.environ.pop("LVDC_PICK_PINNED")
        yt._check_landed("https://www.youtube.com/@AlJazeeraEnglish/live")      # unpinned + unverifiable: recorded only
        assert yt.pin_check["ok"] is None
    finally:
        os.environ.pop("LVDC_PICK_PINNED", None)
        if saved is not None:
            os.environ["LVDC_PICK_PINNED"] = saved


def test_every_reentry_checks_the_sessions_own_room():
    """levi 00:38 M3: after a re-entry confirm the page must still be the session's room / channel, else a named abort."""
    import os
    saved = os.environ.pop("LVDC_PICK_PINNED", None)
    try:
        s = _session("tiktok", Page("https://www.tiktok.com/@other/live"), channel_url=ROOM)       # unpinned tt
        r = {"ok": True}
        s._check_reentry_identity(r)
        assert r["ok"] is False and r["identity"]["landed"] == "@other"
        assert s.abort_reason.startswith("room_mismatch (reentry): wanted @stef_gamingg, landed @other")
        os.environ["LVDC_PICK_PINNED"] = "{}"
        tw = _session("twitch", Page("https://www.twitch.tv/raided_to"), first_url="https://www.twitch.tv/eslcs")
        r = {"ok": True}
        tw._check_reentry_identity(r)
        assert r["ok"] is False and tw.abort_reason.startswith("pin_mismatch (reentry): wanted eslcs, landed raided_to")
        ok = _session("twitch", Page("https://www.twitch.tv/eslcs"), first_url="https://www.twitch.tv/eslcs")
        r = {"ok": True}
        ok._check_reentry_identity(r)
        assert r["ok"] is True and ok.abort_reason is None
        src = (HERE / "browser.py").read_text()
        assert src.count("self._check_reentry_identity(") == 3      # tt room, player error, frozen (yt / tw) re-entries
    finally:
        os.environ.pop("LVDC_PICK_PINNED", None)
        if saved is not None:
            os.environ["LVDC_PICK_PINNED"] = saved


def test_wiring():
    src = (HERE / "browser.py").read_text()
    assert 'if ok and not c.get("fallback"):' in src and '{"dead": "room_mismatch"' in src   # unpinned tt tries
    assert "pin_check=browser.pin_check" in (HERE / "exp.py").read_text()


def main() -> int:
    test_a_room_page_is_never_picked_from()
    test_page_identity()
    test_identity_check_names_a_mismatch_and_never_aborts_unverifiable()
    test_a_landed_mismatch_is_a_named_startup_failure()
    test_every_reentry_checks_the_sessions_own_room()
    test_wiring()
    print("test_pin_identity: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
