#!/usr/bin/env python3
"""B9 shape before join (levi 23:21-23:22; peter's cost / design a57ef9b8): the session's first schedule state is applied
before navigation and the shaper runs through the join; the join's waits scale with that state's rate (per URL
max(45, 1.5 x B / rate), capped at 600 s); a page that attached media but showed no advancing video by the cap is
"join_timeout at state <s> (<n> s)", its own class (never not_live / startup_failure: never set aside, never offline);
the lever's t0 is the first apply after playback starts; the session ends at the schedule's end (navigation + its
length: the join counts inside it), recorded in meta.json join.

    python3 test_b9_join.py        (exit 0 = pass)
"""
import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

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
import entries as E  # noqa: E402
import not_usable  # noqa: E402
import record  # noqa: E402


def test_the_join_timeout_scales_with_the_first_state():
    assert B.join_timeout_s("youtube", 150) == 400.0                  # state 1's floor: 1.5 x 5 MB / 18.75 kB/s
    assert B.join_timeout_s("twitch", 150) == B.JOIN_CAP_S == 600.0   # capped
    assert B.join_timeout_s("tiktok", 700) == round(1.5 * 6.5e6 / (700 * 125), 1)
    assert B.join_timeout_s("youtube", 9000) == B.JOIN_BASE_S == 45.0 # a fast first state: the pre-B9 wait
    assert B.join_timeout_s("youtube", None) == 45.0 and B.join_timeout_s("youtube", 0) == 45.0


def test_join_timeout_is_its_own_class_never_a_channel_problem():
    msg = str(B.JoinTimeout("2", 600))
    assert msg == "join_timeout at state 2 (600 s)" and msg.startswith(B.JOIN_TIMEOUT_PREFIX)
    assert B.JOIN_TIMEOUT_PREFIX == E.JOIN_TIMEOUT_MARK == "join_timeout at state "     # shagun's dry_offline pins it
    assert E.NOT_LIVE_MARK not in msg
    for pf in ("youtube", "twitch", "tiktok"):
        assert E.session_class(pf, "FAIL", msg, [], None, None, None, None) == "join_timeout"
        assert E.session_class(pf, "FAIL", f"Failed to navigate to a live {pf} stream", [], None, None, None, None) == \
            ("not_live" if pf != "tiktok" else "startup_failure")             # a page not live: unchanged
    assert E.CLASS_PRECEDENCE.index("join_timeout") == E.CLASS_PRECEDENCE.index("interrupted") + 1
    with tempfile.TemporaryDirectory() as d:                                  # not_usable never sets a join_timeout aside
        for i, cls in enumerate(("join_timeout", "join_timeout", "startup_failure")):
            sd = Path(d) / "youtube" / f"yt_10000{i}"
            sd.mkdir(parents=True)
            (sd / "check.json").write_text(json.dumps({"session_class": cls}))
            (sd / "meta.json").write_text(json.dumps({"concurrency": {"record": True, "era": record.ERA},
                                                      "started_at": 1000.0 + i, "channel_handle": "@x",
                                                      "channel_url_requested": "https://www.youtube.com/@x/live"}))
        s = not_usable.scan(Path(d), "youtube")
        assert sum(len(v) for v in s.values()) == 1 and not not_usable.set_aside(Path(d), "youtube")


def _exp_fn(name):
    import ast
    src = (HERE / "exp.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(name)


def test_the_session_ends_at_navigation_plus_its_length():
    """levi 23:22: a 300 s join leaves 1500 s of a 1800 s schedule played; the session ends at navigation + 1800 s."""
    import ast
    ns: dict = {}
    exec(compile(ast.Module([_exp_fn("join_record")], []), "exp.join_record", "exec"), ns)
    r = ns["join_record"](1000.0, 1000.0, 1300.0, 1800.0)
    assert r == {"join_s": 300.0, "session_end_planned": 2800.0, "schedule_played_s": 1500.0}
    assert ns["join_record"](1000.0, 1000.1, None, 1800.0) == {"session_end_planned": 2800.0}   # never played
    assert ns["join_record"](1000.0, 1000.0, 3000.0, 1800.0)["schedule_played_s"] == 0.0       # a join past the end


def test_the_first_apply_comes_first_and_the_lever_waits_for_playing():
    """_ossilate (exp.py's own source): the first apply sets JOIN_FIRST with its state and rate; the lever overlay is
    never consulted before JOIN_PLAYING, so its t0 is an apply after playback starts."""
    import test_lever as TL
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class NS:
        rate: int
        state: str
        duration: float
        delay_ms: int = 10
        loss_pct: float = 0.0
        redraw_kbit: int = 0
        redraw_delay_ms: int = 0
        redraw_loss_pct: float = 0.0
        walk_step: int = 0
        episode: int = 0

    class Lever:
        def __init__(self):
            self.calls = []

        def active(self, t):
            self.calls.append(t)
            return None
    lever, applied, first, playing, join = Lever(), [], threading.Event(), threading.Event(), {}
    ns = TL._ossilate_ns(SHAPER_GEN=lambda **kw: iter([NS(300, "2", 0.05), NS(900, "4", 0.05), NS(5000, "8", 0.05)]),
                         _apply_net_stat=lambda net_stat: applied.append((time.time(), net_stat.state)),
                         LEVER=lever, JOIN_FIRST=first, JOIN_PLAYING=playing, JOIN=join)

    async def run():
        task = asyncio.create_task(ns["_ossilate"](episode_time_in_seconds=1, net_labels=[],
                                                   stop_event=asyncio.Event(), transition_dct={}))
        while not first.is_set():
            await asyncio.sleep(0.005)
        assert join["first_state"] == "2" and join["first_rate_kbit"] == 300 and not lever.calls
        playing.set()                                                 # the join thread: playing
        await task
    asyncio.run(run())
    assert [s for _, s in applied] == ["2", "4", "8"]
    assert len(lever.calls) == 2 and lever.calls[0] > join["t_first_apply"]   # the first two applies after playing


class _Page:
    """A page that loads and attaches media but never advances (a starved join)."""

    def __init__(self, attached=True):
        self.attached, self.url = attached, None

    def navigate(self, url):
        self.url = url

    def eval(self, js):
        if js == "location.href":
            return self.url
        if js == "document.readyState":
            return "complete"
        if js == B._CURRENT_TIME_JS:
            return 0.0
        if js == B._MEDIA_ATTACHED_JS:
            return self.attached
        return None


def test_a_starved_join_raises_join_timeout_and_a_dead_page_does_not():
    url = "https://www.youtube.com/@x/live"
    t0 = time.time()
    try:
        B._navigate_to_live(_Page(), "youtube", timeout=0.2, first_url=url, join_state="1")
        raise AssertionError("must raise JoinTimeout")
    except B.JoinTimeout as e:
        assert str(e) == "join_timeout at state 1 (0 s)" and e.detail == {"url": url}
    assert time.time() - t0 < 20
    assert B._navigate_to_live(_Page(attached=False), "youtube", timeout=0.2, first_url=url, join_state="1") is None
    assert B._navigate_to_live(_Page(), "youtube", timeout=0.2, first_url=url) is None     # no join_state: pre-B9


def test_wiring():
    src = (HERE / "exp.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    assert main.index("_join_t.start()") < main.index("abort_reason = asyncio.run(experiment(")
    join = main[main.index("    def _join(browser) -> None:"):main.index("    browser = None\n")]
    call = "\n            browser.start()\n"                         # the statement, not the docstring / comments
    assert join.index("JOIN_FIRST.wait(") < join.index(call) < join.index("JOIN_PLAYING.set()")
    assert "browser.join_scale, browser.join_state = per_url / JOIN_BASE_S" in join
    assert 'abort_check=(lambda: browser.abort_reason or JOIN.get("abort"))' in main
    stmts = [ln.strip() for ln in main.replace(join, "").splitlines() if not ln.strip().startswith("#")]
    assert "browser.start()" not in stmts                               # the join thread is the only start
    bsrc = (HERE / "browser.py").read_text()
    assert "scale=self.join_scale, join_state=self.join_state" in bsrc
    assert "_tt_confirm_playing(page, TT_QUICK_CONFIRM_S * sc)" in bsrc and 'raise JoinTimeout(self.join_state' in bsrc


def main() -> int:
    test_the_join_timeout_scales_with_the_first_state()
    test_join_timeout_is_its_own_class_never_a_channel_problem()
    test_the_session_ends_at_navigation_plus_its_length()
    test_the_first_apply_comes_first_and_the_lever_waits_for_playing()
    test_a_starved_join_raises_join_timeout_and_a_dead_page_does_not()
    test_wiring()
    print("test_b9_join: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
