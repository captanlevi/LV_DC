#!/usr/bin/env python3
"""levi 12:18 (WC3c r3 ti_100003, state 7, scale 1: the room's <video> appeared at +18.7 s, played at +18.9 s, and the
try was called not_playing at +21.3 s with ct 1.41 / tf 42, then raised JoinTimeout at 21 of its 45 s): under B9 a tt
try's confirm runs to the per-URL deadline; JoinTimeout only at that deadline with no first frame; a room that showed
frames but missed the 2 s playhead is a slow_start and the walk takes the next card; every try records dead /
watched_s / ct_advance_s / frames (12:19 (g))."""
from __future__ import annotations

import sys
import tempfile
import threading
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

LOAD_S = 1.3                  # ti_100003: navigate + load before the confirm's click (t_done +21.3 = 1.3 + 5 + 15)


class _Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def ti_100003(t):
    """The room's player at t s after the try's navigate, from ti_100003's player_events (lvqoe_attach +18.70,
    loadeddata +18.88, playing +18.90) and its last sample (+21.3 s: rs 4, ct 1.41, tf 42)."""
    if t < 18.7:
        return {"has_video": False, "src": None, "rs": 0, "ct": None, "tf": None, "paused": True}
    return {"has_video": True, "src": "blob:x", "rs": 4 if t >= 18.9 else 1, "ct": max(0.0, t - 19.9),
            "tf": int(max(0.0, t - 18.9) * 17.5), "paused": False, "dur_inf": True}


def attached_no_frame(t):
    """Media attached and flowing on a live room, no first frame ever (D3's genuine case)."""
    return {"has_video": True, "src": "blob:x", "rs": 1, "ct": 0.0, "tf": 0, "paused": False, "dur_inf": True}


def frozen_first_frame(t):
    """A decoded first frame (tf 1), the playhead never moving, media flowing (brad 734f961d's probe)."""
    return {"has_video": True, "src": "blob:x", "rs": 4, "ct": 0.0, "tf": 1, "paused": False, "dur_inf": True}


def frames_but_slow(t):
    """Frames from +10 s, the playhead crawling (never 2 s by the deadline): a slow start."""
    if t < 10:
        return attached_no_frame(t)
    return {"has_video": True, "src": "blob:x", "rs": 3, "ct": (t - 10) * 0.04, "tf": int((t - 10) * 10),
            "paused": False, "dur_inf": True}


def _session(monkeypatch, rooms: dict, join_state=7, scale=1.0):
    clk = _Clock()
    monkeypatch.setattr(B, "time", clk)
    d = Path(tempfile.mkdtemp()) / "_trio" / "tiktok" / "ti_100003"
    d.mkdir(parents=True)
    b = B.BrowserSession("tiktok", session_dir=d)
    nav = {"room": None, "t": clk.t}

    class _Page:
        def navigate(self, url):
            nav.update(room=url, t=clk.t)

        def eval(self, js, await_promise=False):
            return None
    b._page = _Page()
    b.join_state, b.join_scale = join_state, scale
    b.join_cancel = threading.Event()
    rx = {"n": 0}

    def _rx():
        rx["n"] += 200_000                          # the slot's ingress keeps moving (D3's "flowing")
        return rx["n"]
    b.join_rx = _rx
    monkeypatch.setattr(B, "_wait_for_load", lambda page, timeout=15: clk.sleep(min(LOAD_S, timeout)))
    monkeypatch.setattr(B, "_tt_state", lambda page: rooms[nav["room"]](clk.t - nav["t"]))
    monkeypatch.setattr(B, "publish_stream", lambda *a, **k: None)
    monkeypatch.setattr(B, "identity_check", lambda *a: {"ok": True})
    monkeypatch.setattr(B, "_tt_preroll", lambda *a, **k: (True, {"ct_advance_s": 59.0, "watched_s": 60.0}))
    return b


def _order(*hrefs):
    return [{"href": h, "viewers": 126, "live_badge": True} for h in hrefs]


def _rec():
    return {"tries": [], "choice": None, "candidates": []}


def test_ti_100003_is_accepted_within_its_per_url_budget(monkeypatch):
    room = "https://www.tiktok.com/@georgistru/live"
    b = _session(monkeypatch, {room: ti_100003})
    rec = _rec()
    assert b._tt_try_rooms(rec, _order(room)) == B.ROOM_ROTATION_LIVE_URL
    t = rec["tries"][0]
    assert t["ok"] and rec["choice"] == room and "join_timeout" not in rec
    assert 21.3 < t["t_playing"] - t["t_nav"] < B.JOIN_BASE_S, t        # past the old window's end, inside 45 s
    assert t["confirm_s"] == pytest.approx(B.JOIN_BASE_S - LOAD_S - B.TT_AUTOPLAY_GRACE_S, abs=0.2)


def test_a_flowing_room_with_no_frame_times_out_only_at_the_deadline(monkeypatch):
    room = "https://www.tiktok.com/@noframe/live"
    b = _session(monkeypatch, {room: attached_no_frame})
    rec = _rec()
    with pytest.raises(B.JoinTimeout):
        b._tt_try_rooms(rec, _order(room, "https://www.tiktok.com/@unused/live"))
    t = rec["tries"][0]
    assert t["detail"]["dead"] == "not_playing" and not t["detail"]["showed_frames"]
    assert t["t_done"] - t["t_nav"] >= B.JOIN_BASE_S - 1.0, t           # the full per-URL wait, not 21 s
    assert len(rec["tries"]) == 1 and rec["join_timeout"] == room       # D3: the walk stops (no re-pick by bitrate)


def test_a_frozen_first_frame_is_a_join_timeout_at_the_deadline(monkeypatch):
    """levi 12:18 (b) / 12:50: slow_start needs frames AND ct advance; tf > 0 with ct 0 at the deadline is D3."""
    room = "https://www.tiktok.com/@frozen/live"
    b = _session(monkeypatch, {room: frozen_first_frame})
    rec = _rec()
    with pytest.raises(B.JoinTimeout):
        b._tt_try_rooms(rec, _order(room, "https://www.tiktok.com/@unused/live"))
    t = rec["tries"][0]
    assert len(rec["tries"]) == 1 and t["detail"]["dead"] == "not_playing" and not t["detail"]["showed_frames"]
    assert t["t_done"] - t["t_nav"] >= B.JOIN_BASE_S - 1.0, t


def test_a_slow_start_moves_to_the_next_card_and_every_try_records_its_measures(monkeypatch):
    slow, ok = "https://www.tiktok.com/@slow/live", "https://www.tiktok.com/@ok/live"
    b = _session(monkeypatch, {slow: frames_but_slow, ok: ti_100003})
    rec = _rec()
    assert b._tt_try_rooms(rec, _order(slow, ok)) == B.ROOM_ROTATION_LIVE_URL
    first, second = rec["tries"]
    assert first["detail"]["dead"] == "slow_start" and first["detail"]["showed_frames"] and not first["ok"]
    assert "join_timeout" not in rec and rec["choice"] == ok and second["ok"]
    for k in ("watched_s", "ct_advance_s", "frames"):
        assert first["detail"][k] is not None, k
    assert first["detail"]["frames"] > 0 and 0 < first["detail"]["ct_advance_s"] < 2.0
    assert first["detail"]["ct_trace"] and len(first["detail"]["ct_trace"][0]) == 4      # (t, ct, rs, tf)


def test_a_confirm_that_ends_before_the_deadline_never_raises(monkeypatch):
    """not_playing short of the per-URL deadline (the confirm cut early) is a failed try, the next card; never D3."""
    a, ok = "https://www.tiktok.com/@a/live", "https://www.tiktok.com/@ok/live"
    b = _session(monkeypatch, {a: attached_no_frame, ok: ti_100003})
    real = B._tt_confirm_playing
    monkeypatch.setattr(B, "_tt_confirm_playing", lambda page, s, scale=1.0, **k: real(page, min(s, 10.0), scale=scale))
    rec = _rec()
    assert b._tt_try_rooms(rec, _order(a, ok)) is None                # both cut at 16.3 s: two failed tries, no raise
    assert [t["detail"]["dead"] for t in rec["tries"]] == ["not_playing"] * 2 and "join_timeout" not in rec


def test_off_b9_the_quick_window_stays(monkeypatch):
    room = "https://www.tiktok.com/@georgistru/live"
    b = _session(monkeypatch, {room: ti_100003}, join_state=None)
    rec = _rec()
    assert b._tt_try_rooms(rec, _order(room)) is None                 # 15 s from the click: ct 1.41, as before B9
    assert rec["tries"][0]["confirm_s"] == B.TT_QUICK_CONFIRM_S and rec["tries"][0]["detail"]["dead"] == "slow_start"


def test_an_error_try_still_carries_the_keys(monkeypatch):
    room = "https://www.tiktok.com/@err/live"

    def boom(t):
        raise RuntimeError("cdp gone")
    b = _session(monkeypatch, {room: boom})
    rec = _rec()
    assert b._tt_try_rooms(rec, _order(room)) is None
    d = rec["tries"][0]["detail"]
    assert d["dead"] == "error" and all(k in d for k in ("watched_s", "ct_advance_s", "frames"))


def main() -> int:
    import inspect
    mp = pytest.MonkeyPatch()
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_") and inspect.isfunction(fn):
                fn(mp)
                mp.undo()
    finally:
        mp.undo()
    print("test_tt_join_budget: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
