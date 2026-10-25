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
    """A page for _navigate_to_live: loads (or not), never advances; its join evidence as given (buf_end grows when
    `flowing`)."""

    def __init__(self, attached=True, live=True, flowing=True, loads=True):
        self.ev = {"attached": attached, "dur_inf": False, "badge": live}
        self.flowing, self.loads, self.url, self.buf = flowing, loads, None, 0.0

    def navigate(self, url):
        self.url = url

    def eval(self, js):
        if js == "location.href":
            return self.url
        if js == "document.readyState":
            return "complete" if self.loads else "loading"
        if js == B._CURRENT_TIME_JS:
            return 0.0
        if js == B._JOIN_EVIDENCE_JS:
            self.buf += 1.0 if self.flowing else 0.0
            return json.dumps({**self.ev, "buf_end": self.buf, "decoded": None})
        return None


URL = "https://www.youtube.com/@x/live"


def test_join_timeout_needs_the_full_wait_bytes_flowing_and_live_evidence():
    """levi 23:49 (D3 / Q1): JoinTimeout only with media attached, bytes advancing and live evidence over the full
    scaled wait; a dead page, an offline channel's non-live video, a page with nothing arriving: next URL / None."""
    rec = {}
    try:
        B._navigate_to_live(_Page(), "youtube", timeout=0.2, first_url=URL, join_state="1", record=rec)
        raise AssertionError("must raise JoinTimeout")
    except B.JoinTimeout as e:
        assert str(e).startswith("join_timeout at state 1 (") and e.detail["url"] == URL
        assert e.detail["flowing_by"] == "page_media" and e.detail["live"] and rec["join_tries"][0]["flowing"]
    for page in (_Page(attached=False), _Page(live=False), _Page(flowing=False)):
        assert B._navigate_to_live(page, "youtube", timeout=0.2, first_url=URL, join_state="1") is None, page.ev
    assert B._navigate_to_live(_Page(), "youtube", timeout=0.2, first_url=URL) is None     # no join_state: pre-B9
    due, why = B.join_timeout_due({"attached": True}, {"attached": True, "badge": True}, 1000, 1000 + B.JOIN_RX_MIN_BYTES)
    assert due and why["flowing_by"] == "rx" and why["rx_bytes"] == B.JOIN_RX_MIN_BYTES
    assert not B.join_timeout_due({}, {"attached": True, "badge": True}, 1000, 1000 + B.JOIN_RX_MIN_BYTES - 1)[0]
    assert B.join_timeout_due({}, {"attached": True, "buf_end": 3.0}, None, None, live=True)[0]        # tt: list badge
    assert not B.join_timeout_due({}, {"attached": True, "buf_end": 3.0, "dur_inf": True}, None, None, live=False)[0]


def test_one_deadline_per_url_a_walk_cap_and_a_cancel():
    """D1: every wait clips to the URL's deadline (a page that never loads at scale 40 takes about the deadline, not
    40 x the sum of the waits); the walk stops at JOIN_CAP_S, or at once when cancelled (D2)."""
    t0 = time.time()
    assert B._navigate_to_live(_Page(loads=False, attached=False), "youtube", timeout=0.05, first_url=URL,
                               scale=40.0, join_state="1") is None
    assert time.time() - t0 < 12, time.time() - t0                  # 2 s deadline + the fixed kicks; was > 1 h
    saved = B.JOIN_CAP_S
    try:
        B.JOIN_CAP_S = 0.0
        t0 = time.time()
        assert B._navigate_to_live(_Page(), "youtube", timeout=5.0, join_state="1") is None and time.time() - t0 < 1
    finally:
        B.JOIN_CAP_S = saved
    t0 = time.time()
    assert B._navigate_to_live(_Page(), "youtube", timeout=5.0, first_url=URL, join_state="1",
                               cancel=lambda: True) is None and time.time() - t0 < 1


def test_a_join_still_running_at_the_schedule_end():
    """D2 (levi 23:49): the schedule ended with the join running: the join is cancelled before teardown and the
    session ends "join_timeout at state <s> (<n> s)" (shagun's prefix), recorded, even when the thread never returns."""
    import ast
    fns = [_exp_fn(n) for n in ("join_meta", "join_unfinished_reason", "after_schedule")]
    for stubborn in (False, True):
        join = {"first_state": "2", "t_nav": 1000.0}
        ns = {"JOIN": join, "time": time, "JOIN_STOP_WAIT_S": 0.3}
        exec(compile(ast.Module(fns, []), "exp.after_schedule", "exec"), ns)
        br = type("Br", (), {"join_cancel": threading.Event()})()
        stop_forever = threading.Event()
        t = threading.Thread(target=(stop_forever.wait if stubborn else br.join_cancel.wait), daemon=True)
        t.start()
        wrote = []
        t0 = time.time()
        r = ns["after_schedule"](t, br, None, now=2800.0, write=wrote.append)
        assert r == "join_timeout at state 2 (1800 s)" and r.startswith(B.JOIN_TIMEOUT_PREFIX)
        assert br.join_cancel.is_set() and join["outcome"] == "join_timeout" and join["unfinished"] is True
        assert wrote and wrote[0]["abort"] == r and time.time() - t0 < 2
        assert t.is_alive() == stubborn
        stop_forever.set()
    # brad / levi 00:04: a join thread outliving JOIN_STOP_WAIT_S then finishes and writes: after_schedule's unfinished
    # record stands (the thread's write is skipped), while a normal join still writes its own
    late = {"first_state": "3", "t_nav": 1000.0}
    ns = {"JOIN": late, "time": time, "JOIN_STOP_WAIT_S": 0.2}
    exec(compile(ast.Module(fns + [_exp_fn("join_final_write")], []), "exp.join", "exec"), ns)
    br = type("Br", (), {"join_cancel": threading.Event()})()
    hold = threading.Event()
    th = threading.Thread(target=hold.wait, daemon=True)
    th.start()
    meta = {}
    ns["after_schedule"](th, br, None, now=1900.0, write=lambda j: meta.update(join=j))
    late.update(outcome="error", abort="error: RuntimeError: Failed to navigate to a live youtube stream")   # the thread
    assert ns["join_final_write"](lambda j: meta.update(join=j)) is False
    assert meta["join"]["abort"] == "join_timeout at state 3 (900 s)" and meta["join"]["unfinished"] is True
    hold.set()
    ns["JOIN"] = {"outcome": "playing"}
    assert ns["join_final_write"](lambda j: meta.update(join=j)) is True and meta["join"]["outcome"] == "playing"
    ns = {"JOIN": {"outcome": "playing"}, "time": time, "JOIN_STOP_WAIT_S": 0.3}
    exec(compile(ast.Module(fns, []), "exp.after_schedule", "exec"), ns)
    done = threading.Thread(target=lambda: None)
    done.start()
    assert ns["after_schedule"](done, None, None) is None                 # a finished join: unchanged
    assert ns["after_schedule"](None, None, "x") == "x"                   # manual mode


def test_tt_dead_clocks_scale_during_the_join():
    """levi 23:48: during the join TTRoomWatch's player_dead (readyState 0) and no-progress clocks scale by the join's
    scale, so a slow low-state join is not a dead room; scale 1 (after the join) is unchanged."""
    w1, w4 = B.TTRoomWatch(), B.TTRoomWatch(scale=4.0)
    assert w4.t_dead_s == 4 * w1.t_dead_s and w4.t_max_s == 4 * w1.t_max_s and B.TTRoomWatch(scale=0.5).scale == 1.0
    dead = {"has_video": True, "src": "blob:x", "rs": 0, "ct": 0.0, "buf_end": 0.0, "decoded": 0}
    for w, fire_at in ((w1, w1.t_dead_s), (w4, w4.t_dead_s)):
        got = None
        for k in range(int(fire_at) + 2):
            got = w.feed(dict(dead), 1000.0 + k)
            if got:
                break
        assert got == "player_dead" and k == int(fire_at) + 1, (w.scale, k, got)   # the first sample starts the clock


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
    assert "ok, detail = _tt_confirm_playing(page, confirm_s, scale=sc)" in bsrc and "raise JoinTimeout(self.join_state" in bsrc
    assert "live=bool(c.get(\"live_badge\") or detail.get(\"dur_inf\")))" in bsrc
    assert "rx=self.join_rx, cancel=self.join_cancel.is_set)" in bsrc
    assert "browser.join_rx = lambda: int(_rx_path.read_text())" in src
    assert "abort_reason = after_schedule(_join_t, browser, abort_reason," in main
    assert "join_final_write(lambda j: session_meta.write(session_dir, join=j))" in join   # brad 00:03
    assert 'if not JOIN.get("unfinished"):        # after_schedule already ended it' in join
    assert "_tt_preroll(page, TT_PREROLL_S, scale=sc)" in bsrc


def main() -> int:
    test_the_join_timeout_scales_with_the_first_state()
    test_join_timeout_is_its_own_class_never_a_channel_problem()
    test_the_session_ends_at_navigation_plus_its_length()
    test_the_first_apply_comes_first_and_the_lever_waits_for_playing()
    test_join_timeout_needs_the_full_wait_bytes_flowing_and_live_evidence()
    test_one_deadline_per_url_a_walk_cap_and_a_cancel()
    test_a_join_still_running_at_the_schedule_end()
    test_tt_dead_clocks_scale_during_the_join()
    test_wiring()
    print("test_b9_join: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
