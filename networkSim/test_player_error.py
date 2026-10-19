#!/usr/bin/env python3
"""The player-error recovery (t56 tw_100016, levi's ruling 00128; browser.PlayerErrorWatch / BrowserSession._player_error):
on tw_100016's replay (fixtures/tw_100016_replay.json: the pre-roll stalls, the player is destroyed at 65.5 s, Twitch's
Error #2000 page from 72 s, the "Ad" badge on throughout) the error page triggers ERROR_PAGE_CONFIRM_S after it shows,
and without the page the ad-with-no-player ceiling triggers past AD_NO_PLAYER_MAX_S; a live ad (readyState >= 2) never
triggers; the recovery is the session's one re-entry (reload button, else navigate), recorded in player_dead_reentries
with its t_nav, else the session aborts "player_error" (session_class player_dead; startup_failure keeps precedence);
the ad-wait exposure. Fake page / clock; no browser.

    python3 test_player_error.py        (exit 0 = pass)
"""
import json
import sys
import threading
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402
import check_session as CS  # noqa: E402
import entries as E  # noqa: E402

REPLAY = json.loads((HERE / "fixtures" / "tw_100016_replay.json").read_text())


def _state_at(t: float, error_page: bool = True):
    run = None
    for r in REPLAY["runs"]:
        if r["t0"] <= t + 0.5:
            run = r
    if run is None:
        return None, None
    st = {"ad": run["ad"], "video": run["video"], "rs": run["rs"]}
    return (REPLAY["error"] if error_page and run["error_page"] else None), st


def _replay(error_page: bool = True, until: float = 400.0):
    w = B.PlayerErrorWatch()
    t = 1.0
    while t <= until:
        err, st = _state_at(t, error_page)
        trig = w.step(err, st, t)
        if trig is not None:
            return trig, w
        t += 1.0
    return None, w


def test_the_replay_triggers_on_the_error_page():
    trig, w = _replay()
    assert trig["reason"] == "error_page" and trig["since"] == 72.0, trig
    assert trig["t_trigger"] == 72.0 + B.ERROR_PAGE_CONFIRM_S and trig["error"]["code"] == "2000"
    assert trig["error"]["reload_button"] is True and trig["ad"] is True
    ex = w.exposure()
    assert ex["ad_runs"] == 1 and ex["ad_longest_s"] == trig["t_trigger"] - 4.0, ex      # the ad label from 4 s


def test_without_the_page_the_ad_ceiling_triggers():
    trig, _ = _replay(error_page=False)
    assert trig["reason"] == "ad_no_player" and trig["since"] == 66.0, trig            # readyState 0 from 66 s
    assert trig["t_trigger"] - trig["since"] > B.AD_NO_PLAYER_MAX_S and trig["t_trigger"] == 127.0, trig


def test_a_live_ad_never_triggers():
    w = B.PlayerErrorWatch()
    for t in range(1, 400):           # a long stalled ad, readyState 2, or a playing one: a live player
        assert w.step(None, {"ad": True, "video": True, "rs": 2 if t % 7 else 4}, float(t)) is None
    assert w.exposure()["ad_longest_s"] == 398.0
    w = B.PlayerErrorWatch()
    assert w.step({"code": "2000"}, None, 0.0) is None and w.step(None, None, 2.0) is None   # a flash: not confirmed
    assert w.step({"code": "2000"}, None, 3.0) is None and w.step({"code": "2000"}, None, 8.0)["reason"] == "error_page"
    w.reset()
    assert w.step({"code": "2000"}, None, 9.0) is None                                        # starts over after a recovery


class _Page:
    def __init__(self, clicked=True):
        self.url, self.clicked, self.navigated = "https://www.twitch.tv/chan", clicked, []

    def eval(self, js, await_promise=False):
        if js == "location.href":
            return self.url
        if js == B._PLAYER_ERROR_RELOAD_JS:
            return json.dumps({**REPLAY["error"], "reload_button": self.clicked})
        return None

    def navigate(self, url):
        self.navigated.append(url)


def _session(clicked=True):
    s = B.BrowserSession.__new__(B.BrowserSession)
    s.platform, s.abort_reason, s.abort_at, s.channel_url = "twitch", None, None, "https://www.twitch.tv/chan"
    s.frozen_reentries, s.player_dead_reentries, s.player_errors = [], [], []
    s._recovery_lock, s._page = threading.Lock(), _Page(clicked)
    s._after_reentry_setup = lambda: {"ok": True}
    return s


def test_the_recovery_and_the_abort():
    trig, _ = _replay()
    saved = (B._confirm_advancing, B._wait_for_load)
    try:
        B._wait_for_load = lambda page, timeout=15: None
        B._confirm_advancing = lambda page, s: (True, {"ct": 12.0})
        s = _session()
        s._player_error(trig)                                     # the reload button: playing again
        r = s.player_dead_reentries[0]
        assert s.abort_reason is None and r["method"] == "reload_button" and r["outcome"] == "continue", r
        assert r["reason"] == "error_page" and r["t_nav"] and not s._page.navigated
        assert s.player_errors[0]["reentry"]["outcome"] == "continue"
        s._player_error(trig)                                     # a second error: the one recovery is used
        assert s.abort_reason == "player_error" and s.player_errors[-1]["abort"] == "recovery_used"
        B._confirm_advancing = lambda page, s: (False, {"not_playing": True})
        s = _session(clicked=False)                               # no button (yt): navigate; not playing: abort
        s._player_error({**trig, "error": {**trig["error"], "reload_button": False}})
        r = s.player_dead_reentries[0]
        assert r["method"] == "navigate" and s._page.navigated == ["https://www.twitch.tv/chan"], r
        assert r["outcome"] == "player_error" and s.abort_reason == "player_error"
        assert s.player_errors[0]["abort"] == "reentry_not_playing"
        s = _session()
        s.frozen_reentries = [{"outcome": "continue"}]            # a frozen re-entry already used the recovery
        s._player_error(trig)
        assert not s.player_dead_reentries and s.abort_reason == "player_error"
    finally:
        B._confirm_advancing, B._wait_for_load = saved


def test_the_class_and_the_reason():
    stream = [{"kind": "stream", "sustained_60s": True}]
    assert E.session_class("twitch", "FAIL", "player_error", stream, None, None, None, None) == "player_dead"
    never = [{"kind": "stream", "sustained_60s": False}]          # tw_100016 never played 60 s: startup_failure kept
    assert E.session_class("twitch", "FAIL", "player_error", never, None, None, None, None) == "startup_failure"
    v, why = CS.verdict({}, {}, {}, [], 0.25, "player_error")
    assert any(w.startswith("player_error (yt / twitch") for w in why), why
    _, why = CS.verdict({}, {}, {}, [], 0.25, "player_dead")      # tt's reason text unchanged (its kind in the tallies)
    assert "player_dead (tt: readyState 0 with no media for T; browser.py TTRoomWatch)" in why


def test_the_watcher_loop_calls_the_recovery():
    """_ad_watcher_loop polls the error JS and the ad / player JS each second and hands a trigger to the session."""
    stop, got = threading.Event(), []

    class P:
        def eval(self, js, await_promise=False):
            if js == B._PLAYER_ERROR_CHECK_JS:
                return json.dumps(REPLAY["error"])
            if js == B._AD_PLAYER_JS:
                return json.dumps({"ad": True, "video": True, "rs": 0})
            return None
    th = threading.Thread(target=B._ad_watcher_loop, args=(P(), "twitch", stop),
                          kwargs={"on_player_error": lambda trig: (got.append(trig), stop.set()),
                                  "error_watch": B.PlayerErrorWatch(confirm_s=0.0)}, daemon=True)
    th.start()
    th.join(timeout=10)
    assert got and got[0]["reason"] == "error_page", got
    src = (HERE / "browser.py").read_text()
    assert "self._player_error if self.platform in PLAYER_ERROR_PLATFORMS else None, self.error_watch" in src
    assert "player_errors=browser.player_errors, ad_wait=browser.error_watch.exposure()" in (HERE / "exp.py").read_text()


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_player_error: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
