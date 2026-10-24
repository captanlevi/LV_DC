#!/usr/bin/env python3
"""WA2 r1 sweep (levi / supervisor 00:27): a collector constant and every consumer threshold tied to it come from ONE
source or are parity-tested, and the re-entry confirms need decoded frames (levi 00:28 (5a)).
- the shot cadence: browser.SHOT_INTERVAL_S IS check_session.CHECKS["shot_interval_s"] (10 s there sent every v3
  session to _failed: "screenshots 24 of ~151 expected");
- the low states: check_session.LOW_STATE_MAX == utils/netStat._LOW_STATE_THRESHOLD;
- the frozen tolerance and the frozen-picture frame floor: check_session == content.js;
- the A8 screen probe no longer shadows window_fullscreen's _SCREEN_JS (C-1);
- _confirm_advancing / _tt_confirm_playing: a clock advancing with no decoded frame is not "playing".

    python3 test_consumer_parity.py        (exit 0 = pass)
"""
import json
import re
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
import check_session as CS  # noqa: E402
from utils import netStat as NS  # noqa: E402

JS = (HERE.parent / "labeling" / "content.js").read_text()


def _js_const(name):
    return float(re.search(rf"const {name} = ([0-9.]+);", JS).group(1))


def test_one_shot_cadence():
    assert B.SHOT_INTERVAL_S == CS.CHECKS["shot_interval_s"] == 60.0
    assert 'SHOT_INTERVAL_S = _CS_CHECKS["shot_interval_s"]' in (HERE / "browser.py").read_text()   # imported, not copied


def test_low_states_and_frozen_constants_agree():
    assert CS.LOW_STATE_MAX == NS._LOW_STATE_THRESHOLD
    assert CS.FROZEN_PLAYHEAD_TOL_S == _js_const("FROZEN_TOL_S")
    assert CS.FROZEN_FRAMES_MAX == _js_const("FROZEN_FRAMES_MAX")


def test_screen_probes_are_distinct():
    assert "dpr" in B._SCREEN_JS and "Promise" not in B._SCREEN_JS          # window_fullscreen's synchronous probe
    assert "refresh_hz" in B._A8_SCREEN_JS and "Promise" in B._A8_SCREEN_JS


class _Page:
    def __init__(self, tf_step):
        self.ct, self.tf, self.tf_step = 0.0, 100, tf_step

    def eval(self, js, await_promise=False):
        if js == B._PLAYER_STATE_JS:
            self.ct += 1.0
            self.tf += self.tf_step
            return json.dumps({"paused": False, "rs": 4, "tf": self.tf})
        if js == B._CURRENT_TIME_JS:
            return self.ct
        if js == B._TT_PLAYER_STATE_JS:
            self.ct += 1.0
            self.tf += self.tf_step
            return json.dumps({"has_video": True, "src": True, "rs": 4, "ct": self.ct, "tf": self.tf, "paused": False})
        return None


def _quick(fn, *a):
    saved = B.time.sleep
    try:
        B.time.sleep = lambda s: None
        return fn(*a)
    finally:
        B.time.sleep = saved


def test_confirms_need_decoded_frames():
    ok, d = _quick(B._confirm_advancing, _Page(0), 0.3)        # ti_100001: clock runs, no frame: never "playing"
    assert not ok and d["frames"] == 0
    ok, d = _quick(B._confirm_advancing, _Page(30), 0.3)
    assert ok and d["frames"] >= B.CONFIRM_MIN_FRAMES
    saved = B.TT_AUTOPLAY_GRACE_S
    try:
        B.TT_AUTOPLAY_GRACE_S = 0
        ok, d = _quick(B._tt_confirm_playing, _Page(0), 0.3)
        assert not ok and d["frames"] == 0
        ok, d = _quick(B._tt_confirm_playing, _Page(30), 0.3)
        assert ok and d["frames"] >= B.CONFIRM_MIN_FRAMES
    finally:
        B.TT_AUTOPLAY_GRACE_S = saved
    g = B._FramesGate()                                         # never readable: the clock decides, recorded
    g.feed(None)
    assert g.ok() and g.record()["frames_unreadable"]
    g = B._FramesGate()                                         # a reset restarts the count
    for tf in (500, 501, 0, 1):
        g.feed(tf)
    assert not g.ok()
    g.feed(3)
    assert g.ok()


def main() -> int:
    test_one_shot_cadence()
    test_low_states_and_frozen_constants_agree()
    test_screen_probes_are_distinct()
    test_confirms_need_decoded_frames()
    print("test_consumer_parity: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
