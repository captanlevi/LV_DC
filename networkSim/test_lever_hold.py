#!/usr/bin/env python3
"""v3 B4 hold fix (levi 21:12; a B4 defect found before any lever window ran): every abort / re-entry hold keyed on the
shaper STATE (stuck.LOW_HOLD_STATES, browser.TT_LOW_STATES) also holds inside a lever episode, whatever the scheduled
state, with the same grace after it: stuck.is_low (low_hold_end / low_spans: browser._lowstate_held, the error-watch
hold, _stuck_tick, _on_frozen, exp.py's player_error_held, the readers entries / check_session) and tt's TTRoomWatch
(dead clock + rule b, lever_active from the live label). A plain state-9 row is not held (C1's starvation logic handles
a state-9 stall with data, unchanged). net_labels.txt rows carry "lever": null outside an episode (absent = pre-B4).

    python3 test_lever_hold.py        (exit 0 = pass)
"""
import inspect
import sys
import types
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402
import stuck  # noqa: E402
from utils.dataModels import NetLabel  # noqa: E402


def _nl(t, state, lever=None):
    return NetLabel(timestamp=t, speed=5000, state=state, lever=lever)


def test_is_low():
    assert stuck.is_low(_nl(1.0, "9", 1)) and not stuck.is_low(_nl(1.0, "9"))
    assert stuck.is_low({"state": "9", "lever": 2}) and not stuck.is_low({"state": "9", "lever": None})
    assert not stuck.is_low({"state": "9"}) and stuck.is_low({"state": "3"})          # pre-B4 row: by its state
    assert stuck.is_low(_nl(1.0, "2")) and not stuck.is_low({"state": None})
    assert asdict(_nl(1.0, "9"))["lever"] is None                                       # written as null, not absent


def test_hold_end_and_spans():
    labs = [_nl(0.0, "9"), _nl(10.0, "9", 1), _nl(40.0, "9")]
    assert stuck.low_hold_end(labs, 5.0) is None                                        # before the episode: unheld
    assert stuck.low_hold_end(labs, 20.0) == 20.0 + stuck.LOW_HOLD_GRACE_S              # inside: runs to t + grace
    assert stuck.low_hold_end(labs, 50.0) == 40.0 + stuck.LOW_HOLD_GRACE_S              # after: the grace
    assert stuck.low_spans(labs) == [(10.0, 40.0)]
    plain = [_nl(0.0, "9"), _nl(10.0, "9"), _nl(40.0, "9")]
    assert stuck.low_hold_end(plain, 20.0) is None and stuck.low_spans(plain) == []
    dicts = [{"timestamp": 0.0, "state": "9", "lever": None}, {"timestamp": 10.0, "state": "9", "lever": 1},
             {"timestamp": 40.0, "state": "9", "lever": None}]
    assert stuck.low_spans(dicts) == [(10.0, 40.0)]                                      # the readers' net_labels.txt


def test_browser_hold_reads_lever():
    b = B.BrowserSession.__new__(B.BrowserSession)
    b.net_labels = [_nl(1000.0, "9"), _nl(1010.0, "9", 3), _nl(1040.0, "9")]
    assert not b._lowstate_held(1005.0) and b._lowstate_held(1020.0)
    assert b._lowstate_held(1040.0 + stuck.LOW_HOLD_GRACE_S - 1) and not b._lowstate_held(1040.0 + stuck.LOW_HOLD_GRACE_S + 1)


def test_tt_dead_clock_holds_in_an_episode():
    dead = {"has_video": True, "src": True, "rs": 0, "ct": 0, "buf_end": None, "decoded": None, "stall_label": True}
    w = B.TTRoomWatch()
    assert not any(w.feed(dead, 1000.0 + i, shaper_state=9, lever_active=True) for i in range(300))
    w, fired = B.TTRoomWatch(), None
    for i in range(300):                                               # 100 s of episode at state 9, then none
        r = w.feed(dead, 2000.0 + i, shaper_state=9, lever_active=i < 100)
        if r:
            fired = (r, i)
            break
    assert fired and fired[0] == "player_dead" and abs(fired[1] - (99 + B.TT_STALL_GRACE_S + 1 + 60)) <= 1, fired
    w = B.TTRoomWatch()                                                # state 9, no episode: unchanged (62 s)
    assert next(i for i in range(100) if w.feed(dead, 4000.0 + i, shaper_state=9)) == 61
    src = inspect.getsource(B.BrowserSession._room_watch_loop)
    assert 'getattr(labels[-1], "lever", None) is not None' in src and "lever_active=lever_on" in src


def test_c1_unchanged():
    src = inspect.getsource(B.SelfPauseResumer)
    assert "is_low" not in src and "low_hold_end" not in src and "lever" not in src


def main() -> int:
    test_is_low()
    test_hold_end_and_spans()
    test_browser_hold_reads_lever()
    test_tt_dead_clock_holds_in_an_episode()
    test_c1_unchanged()
    print("test_lever_hold: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
