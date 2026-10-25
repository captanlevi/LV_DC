"""levi 06:40 (WC3 r1 ti_100001: 4 live rooms skipped at 204 kbit by `advance >= seconds / 2`, the lowest-viewer room
played): the tt pre-roll accepts a live room whose playhead advances >= TT_PREROLL_MIN_ADV_S within the scaled pre-roll,
at every state alike; the advance share is a recorded covariate, never a filter."""
from __future__ import annotations

import sys
import threading
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


class _Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(rate, seconds=60.0, scale=1.0, dead_at=None, cancel_at=None, until=None, start_at=0.0):
    """A fake tt page whose playhead advances `rate` s per wall second from `start_at` s on."""
    clk, saved = _Clock(), (B.time, B._tt_state, B.TTRoomWatch)
    t0, ev = clk.t, threading.Event()

    class _W:
        def __init__(self, scale=1.0):
            pass

        def feed(self, s, now):
            return "room_ended" if dead_at is not None and now - t0 >= dead_at else None

    def state(_page):
        el = clk.t - t0
        if cancel_at is not None and el >= cancel_at:
            ev.set()
        return {"ct": 100.0 + rate * max(0.0, el - start_at), "buf_end": None, "decoded": None}
    B.time, B._tt_state, B.TTRoomWatch = clk, state, _W
    try:
        return B._tt_preroll(None, seconds, scale=scale, cancel=ev, until=None if until is None else t0 + until)
    finally:
        B.time, B._tt_state, B.TTRoomWatch = saved


def test_a_slow_live_room_is_accepted_at_any_state():
    ok, d = _run(rate=0.2)                                  # ti_100001's rooms: ~12 s per 60 s at 204 kbit
    assert ok and d["ct_advance_s"] >= 10 and 0.15 < d["advance_share"] < 0.25 and not d["extended"], d
    ok_fast, d_fast = _run(rate=1.0)
    assert ok_fast and d_fast["advance_share"] > 0.9       # the same acceptance; only the covariate differs
    assert "preroll_short" not in str(d) and "dead" not in d


def test_no_advance_waits_the_scaled_preroll_then_fails():
    ok, d = _run(rate=0.0, seconds=60.0, scale=3.0)
    assert not ok and d["dead"] == "preroll_no_advance" and d["extended"] and 179 <= d["watched_s"] <= 182, d
    ok, d = _run(rate=0.5, seconds=60.0, scale=3.0, start_at=100.0)  # its first second arrives inside the extension
    assert ok and d["extended"] and 101 <= d["watched_s"] <= 104, d
    ok, d = _run(rate=0.0, seconds=60.0, scale=1.0)        # unscaled: no extension beyond the pre-roll
    assert not ok and not d["extended"], d


def test_dead_cancel_and_cap():
    ok, d = _run(rate=1.0, dead_at=10)
    assert not ok and d["dead"] == "room_ended"
    ok, d = _run(rate=0.0, seconds=60.0, scale=10.0, cancel_at=90)
    assert not ok and d["watched_s"] < 95, d               # the join's cancel stops the extension
    ok, d = _run(rate=0.0, seconds=60.0, scale=10.0, until=120)
    assert not ok and d["watched_s"] < 125, d              # clipped to the join walk's cap
    ok, d = _run(rate=1.0, seconds=60.0, scale=1.0, until=30)
    assert ok and d["watched_s"] < 32, d                   # a cap inside the base pre-roll ends it there (no overrun)
    ok, d = _run(rate=0.0, seconds=60.0, scale=1.0, until=30)
    assert not ok and d["watched_s"] < 32, d


def main() -> int:
    test_a_slow_live_room_is_accepted_at_any_state()
    test_no_advance_waits_the_scaled_preroll_then_fails()
    test_dead_cancel_and_cap()
    print("test_tt_preroll: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
