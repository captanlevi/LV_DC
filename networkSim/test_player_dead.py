#!/usr/bin/env python3
"""tt player_dead (supervisor 2026-10-17, ti_20 / ti_21) and levi's session_class / entries ruling.

- ti_20's real ticks (fixtures/tt_ti20_*: played 110 s, then 'end' and 722 s at 0x0 / playhead 0 under the stall label;
  the 720 s player_frozen watchdog ended it) replayed through browser.TTRoomWatch: player_dead fires 60 s into the dead
  run, not at 720 s, and no room_ended rule fires first.
- A dead run that recovers inside 60 s is a discontinuity, never a cut.
- entries.py: ti_20 has one stream entry, unshaped, the first shaped state after it, sustained; its dead run;
  session_class precedence; startup rates per entry state.

    python3 test_player_dead.py        (exit 0 = pass)
"""
import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402
import entries as E  # noqa: E402

FX = HERE / "fixtures"
_TICKS = json.loads((FX / "tt_ti20_ticks_2026-10-17.json").read_text())
ROWS = [dict(zip(_TICKS["columns"], r)) for r in _TICKS["rows"]]
SESS = json.loads((FX / "tt_ti20_session_2026-10-17.json").read_text())


def samples(rows):
    """TTRoomWatch samples from ticks: readyState from the tick (old ticks: 0 at 0x0 / playhead 0, else 4)."""
    out, stall = [], False
    for r in rows:
        ty = r["type"]
        stall = True if ty == "stall" else False if ty in ("start", "video_start") else stall
        if ty != "res_meta":
            continue
        ct = E._f(r["current_time"])
        dead = E._f(r["video_w"]) in (0.0, None) and ct in (0.0, None)
        ahead = E._f(r.get("buffered_ahead_s"))
        out.append((float(r["ts"]) / 1000, {"has_video": True, "src": True, "rs": 0 if dead else 4, "ct": ct,
                                            "buf_end": None if dead else (ct or 0) + (ahead or 0), "decoded": None,
                                            "ended_text": False, "stall_label": stall}))
    return out


def test_ti20_replay():
    t_shaped = min(n["timestamp"] for n in SESS["net_labels"])      # the room watch runs after the pre-roll
    t0 = float(ROWS[0]["ts"]) / 1000
    w, fired = B.TTRoomWatch(), None
    for t, s in samples(ROWS):
        if t < t_shaped:
            continue
        state = E.shaper_at(SESS["net_labels"], t)["state"]
        r = w.feed(s, t, shaper_state=int(state) if state.isdigit() else None)
        if r:
            fired = (r, round(t - t0, 1))
            break
    # dead run from 111.5 s; levi 2026-10-21: the clock holds in states 1-4 (ti_20 sat in 2-4 until 621.6 s) and 30 s
    # after, so player_dead fires 60 s after 651.6 s (it fired at ~172 s before the hold)
    assert fired and fired[0] == "player_dead" and 710 <= fired[1] <= 715, fired
    assert SESS["abort_reason"] == "player_frozen"                                   # what happened without the rule


def test_recovery_is_a_discontinuity():
    def run(dead_s):
        w, t, out = B.TTRoomWatch(), 1000.0, None
        live = lambda ct: {"has_video": True, "src": True, "rs": 4, "ct": ct, "buf_end": ct + 3, "decoded": int(ct * 1e4)}
        for i in range(10):
            out = out or w.feed(live(float(i)), t + i)
        t += 10
        for i in range(dead_s):
            out = out or w.feed({"has_video": True, "src": True, "rs": 0, "ct": 0, "buf_end": None, "decoded": None,
                                 "stall_label": True}, t + i)
        t += dead_s
        for i in range(10):
            out = out or w.feed(live(20.0 + i), t + i)
        return out, w.discontinuities
    out, disc = run(20)
    assert out is None and len(disc) == 1 and 19 <= disc[0]["duration_s"] <= 21, (out, disc)
    assert run(59)[0] is None
    assert run(62)[0] == "player_dead"
    # no element at all counts the same; media still arriving (buffered end moving) never does
    w = B.TTRoomWatch()
    assert any(w.feed({"has_video": False}, 2000.0 + i) == "player_dead" for i in range(62))
    w = B.TTRoomWatch()
    assert not any(w.feed({"has_video": True, "src": True, "rs": 0, "buf_end": float(i), "decoded": None}, 3000.0 + i)
                   for i in range(100))


def test_low_state_hold():
    """levi 2026-10-21 (t58 w2 stop-and-read): in TT_LOW_STATES a readyState-0 player with bytes arriving is the shaping;
    the player_dead clock holds there and for TT_STALL_GRACE_S after leaving them; a death that persists after the
    state rises fires once the grace and T have passed; outside the low states nothing changes."""
    dead = {"has_video": True, "src": True, "rs": 0, "ct": 0, "buf_end": None, "decoded": None, "stall_label": True}
    w, out = B.TTRoomWatch(), None
    for i in range(600):                                                # 10 min dead at state 1: no player_dead
        out = out or w.feed(dead, 1000.0 + i, shaper_state=1)
    assert out is None, out
    for st in (2, 3, 4):
        w = B.TTRoomWatch()
        assert not any(w.feed(dead, 2000.0 + i, shaper_state=st) for i in range(200)), st
    w, fired = B.TTRoomWatch(), None
    for i in range(300):                                                # 100 s at state 2, then state 6, still dead
        r = w.feed(dead, 3000.0 + i, shaper_state=2 if i < 100 else 6)
        if r:
            fired = (r, i)
            break
    # the last low sample at i = 99; held to i = 99 + TT_STALL_GRACE_S; T = 60 s after that
    assert fired and fired[0] == "player_dead" and abs(fired[1] - (99 + B.TT_STALL_GRACE_S + 1 + 60)) <= 1, fired
    w = B.TTRoomWatch()                                                 # outside the low states: unchanged (62 s)
    assert next(i for i in range(100) if w.feed(dead, 4000.0 + i, shaper_state=7)) == 61
    w = B.TTRoomWatch()                                                 # no state known (unshaped): unchanged
    assert any(w.feed(dead, 5000.0 + i) == "player_dead" for i in range(62))


def test_room_ended_carries_the_reentry_confirm():
    """bundle item 4 (supervisor 2026-10-21, ti_100037): room_ended after a re-entry keeps the pre-re-entry sample and
    carries the re-entry's own confirmation beside it, on both re-entry paths."""
    r = {"t_nav": 10.0, "t_enter": 13.0, "confirm": {"dead": "ended_page", "rs": None, "ct": None, "ended_text": True},
         "samples": 6, "samples_no_video": 4, "outcome": "room_ended", "room": "https://www.tiktok.com/@a/live",
         "dead_window": {"stream": "alive"}}
    c = B.reentry_confirm_of(r)
    assert c == {"t_nav": 10.0, "t_enter": 13.0, "confirm": r["confirm"], "samples": 6, "samples_no_video": 4,
                 "outcome": "room_ended", "room": r["room"]}
    assert B.reentry_confirm_of(None)["confirm"] is None
    src = (HERE / "browser.py").read_text()
    assert src.count('"reentry_confirm": reentry_confirm_of(r)') == 2       # the player_dead and the frozen re-entry


def test_preroll_ad_sustains_the_entry():
    """bundle item 5 (restart prereg fe48beec, tw_100051): an unpaused advancing ad sustains the stream entry; a paused
    ad does not; dead_runs' played_at (sustained play, not the entry) still needs non-ad play."""
    def rows(ad_paused=False):
        out = []
        for k in range(120):
            ad = 3 <= k < 70
            out.append({"ts": str((1000.0 + k) * 1000), "type": "res_meta", "current_time": str(0 if ad and ad_paused else k),
                        "is_ad": str(ad), "paused": str(ad and ad_paused), "url": "https://www.twitch.tv/x"})
        return out
    meta = {"channel_url": "https://www.twitch.tv/x", "platform": "twitch"}
    e = E.compute_entries(rows(), meta, [])[0]
    assert e["sustained_60s"] and e["sustained_at"] == 1006.0, e
    assert E.sustained_at(rows(), 1000.0) is None                         # the default (not the entry): ads are no play
    assert not E.compute_entries(rows(ad_paused=True), meta, [])[0]["sustained_60s"]
    assert E.sustained_at(rows(), 0.0, window_s=float("inf")) == 1075.0    # played_at: non-ad play after the ad


def test_entries_and_class():
    meta = {"channel_url": SESS["channel_url"], "room_rotation": SESS["room_rotation"]}
    ents = E.compute_entries(ROWS, meta, SESS["net_labels"])
    assert [e["kind"] for e in ents] == ["stream"], ents
    e = ents[0]
    first = min(SESS["net_labels"], key=lambda n: n["timestamp"])
    assert e["shaper_at_entry"] == {"state": "unshaped", "rate_kbit": None}
    assert e["first_shaped_after"]["state"] == str(first["state"]) and e["first_shaped_after"]["rate_kbit"] == first["speed"]
    assert e["sustained_60s"] and e["sustained_at"] - e["t"] < 20, e
    dead = E.dead_player_run(ROWS)
    assert 715 <= dead <= 725, dead
    assert E.session_class("tiktok", "FAIL", "player_frozen", ents, dead, 60, 722, 720) == "player_dead"


def test_precedence():
    ok = [{"kind": "stream", "sustained_60s": True}]
    bad = [{"kind": "stream", "sustained_60s": False}]
    C = E.session_class
    nl = "error: RuntimeError: Failed to navigate to a live youtube stream"
    assert C("youtube", "FAIL", nl, [], None, None, None, None) == "not_live"
    assert C("twitch", "FAIL", nl.replace("youtube", "twitch"), [], None, None, None, None) == "not_live"
    assert C("tiktok", "FAIL", nl.replace("youtube", "tiktok"), [], None, None, None, None) == "startup_failure"
    assert C("tiktok", "FAIL", "login_wall", bad, None, None, None, None) == "login_wall"
    assert C("tiktok", "FAIL", "signal:SIGTERM", bad, None, None, None, None) == "interrupted"
    assert C("tiktok", "FAIL", "room_ended", bad, None, None, None, None) == "startup_failure"
    assert C("tiktok", "FAIL", "room_ended", ok, 100, 60, None, None) == "room_ended"
    assert C("tiktok", "FAIL", "player_dead", ok, None, 60, 800, 720) == "player_dead"
    assert C("tiktok", "FAIL", None, ok, 30, 60, 800, 720) == "player_frozen"
    assert C("youtube", "FAIL", None, ok, None, None, 10, 180) == "qa_fail"
    assert C("youtube", "PASS", None, ok, None, None, 10, 180) == "normal"
    assert list(E.CLASS_PRECEDENCE) == ["not_live", "login_wall", "interrupted", "startup_failure", "room_ended",
                                        "player_dead", "player_dead_recovered", "player_frozen",
                                        "player_frozen_recovered", "qa_fail", "normal"]
    assert E.RECOVERED_DEAD_CLASS == "normal"          # levi (a); player_dead_recovered is read only under option (b)


def test_rates_and_reentry():
    nl = [{"timestamp": 100.0, "speed": 300, "state": "2"}, {"timestamp": 160.0, "speed": 900, "state": "4"}]
    rows = []
    def tick(t, ct, url):
        rows.append({"type": "res_meta", "ts": str(t * 1000), "current_time": str(ct), "url": url, "video_w": "720"})
    rows.append({"type": "start", "ts": "0"})
    for i in range(0, 90):                       # room a plays from t=0
        tick(float(i), float(i), "a")
    for i in range(130, 200):                    # room b (entered at 125 in state 2) never advances
        tick(float(i), 0.0, "b")
    meta = {"channel_url": "a", "room_rotation": {"rooms": [{"room": "a", "first": True},
                                                             {"room": "b", "t_nav": 120.0, "t_enter": 125.0, "ok": False}]}}
    ents = E.compute_entries(rows, meta, nl)
    assert [(e["kind"], e["shaper_at_entry"]["state"], (e["first_shaped_after"] or {}).get("state"), e["sustained_60s"])
            for e in ents] == [("stream", "unshaped", "2", True), ("room_reentry", "2", "4", False)], ents
    assert E.startup_rates(ents) == {"stream:unshaped": {"entries": 1, "failed": 0, "rate": 0.0},
                                     "room_reentry:2": {"entries": 1, "failed": 1, "rate": 1.0}}


def test_tt_start_failure_entry():
    """levi: a tt start failure is a failed entry in the unshaped row, with or without ticks."""
    meta = {"platform": "tiktok", "abort_reason": "error: RuntimeError: Failed to navigate to a live tiktok stream",
            "started_at": 50.0, "channel_url": None}
    e = E.compute_entries([], meta, [])
    assert [(x["kind"], x["shaper_at_entry"]["state"], x["sustained_60s"], x.get("start_failed")) for x in e] == \
        [("stream", "unshaped", False, True)], e
    rows = [{"type": "start", "ts": "0"}] + [{"type": "res_meta", "ts": str((60 + i) * 1000), "current_time": str(i),
                                              "url": "x", "video_w": "720"} for i in range(20)]
    e = E.compute_entries(rows, meta, [])           # a tried room played 20 s before the picker gave up
    assert len(e) == 1 and not e[0]["sustained_60s"] and e[0]["start_failed"], e
    assert E.startup_rates(e) == {"stream:unshaped": {"entries": 1, "failed": 1, "rate": 1.0}}
    yt = {**meta, "platform": "youtube", "abort_reason": meta["abort_reason"].replace("tiktok", "youtube")}
    assert E.compute_entries([], yt, []) == []     # yt / twitch: not_live, no entry made up


def test_ti32_dead_after_end_with_the_stream_alive():
    """chaos ti_32 (2026-10-18, fixture: the last 130 s of ticks, per-second shaped downstream from the pcap, the shaper):
    sustained play, the element's 'end' + 'pause', then readyState 0 / 0x0 / playhead 0 while the media flow kept
    arriving at about the shaper rate. player_dead fires 60 s into the run (no shaped stall precedes it: the
    supervisor's hypothesis does not fit), and the dead window reads stream "alive"."""
    fx = json.loads((FX / "tt_ti32_player_dead_2026-10-18.json").read_text())
    rows = [dict(zip(fx["columns"], r)) for r in fx["rows"]]
    at = fx["abort_at"]
    ev = [(float(r["ts"]) / 1000 - at, r["type"]) for r in rows if r["type"] != "res_meta"]
    end_t = max(t for t, ty in ev if ty == "end")
    assert any(ty == "pause" and 0 <= t - end_t < 1 for t, ty in ev)
    cum, srx_by_s = 0, {}
    for k in range(-130, 1):
        cum += fx["rx_bytes_per_s"][str(k)]
        srx_by_s[k] = cum
    w, fired = B.TTRoomWatch(), None
    stall = False
    for r in rows:
        stall = True if r["type"] == "stall" else False if r["type"] in ("start", "video_start") else stall
        if r["type"] != "res_meta":
            continue
        t = float(r["ts"]) / 1000
        rs = int(float(r["ready_state"])) if r.get("ready_state") not in (None, "", "None") else None
        ct = E._f(r["current_time"])
        s = {"has_video": True, "src": True, "rs": rs, "ct": ct, "buf_end": None if rs == 0 else (ct or 0) + 3,
             "decoded": None, "stall_label": stall}
        if w.feed(s, t, shaped_rx_bytes=srx_by_s.get(int(t - at))) == "player_dead":
            fired = t - at
            break
    # the ticks stop 1.2 s before the abort; the watcher's own samples (meta sample: the same dead state) run on to it
    t = float(rows[-1]["ts"]) / 1000
    while fired is None and t < at + 1:
        t += 1.0
        if w.feed(s, t, shaped_rx_bytes=srx_by_s.get(min(int(t - at), 0))) == "player_dead":
            fired = t - at
    # 60 s into the dead run (the replay's 1 Hz sampling adds up to 2 s), about 2 s after the element's 'end'
    assert fired is not None and -3 <= fired <= 2 and 58 <= fired - end_t <= 64, (fired, end_t)
    shaper = fx["shaper"][-1][1]
    dw = B.dead_window(w.srx, shaper)
    assert dw["stream"] == "alive" and dw["share_of_shaper"] > 0.8, dw
    # the same run with only chat-level bytes arriving (ti_21: about 122 kbit/s at 952) reads no_media
    assert B.dead_window([(0.0, 0), (60.0, int(122 * 1000 / 8 * 60))], 952)["stream"] == "no_media"
    assert B.dead_window([(0.0, 0)], 952)["stream"] is None


def test_ti43_dead_window_reference():
    """peter's ti_43 read (2026-10-18, HEVC): at the second death the FLV stream arrived at about 2 Mbit/s through the dead
    run (pcap) under shaper state 8 (3636 kbit); the shaped rx was 1729.4 kbit/s, 0.476 of the shaper rate, so the share
    of the shaper alone read no_media. The reference is min(shaper rate, the pre-dead playing rate): alive. Because the
    reference never exceeds the shaper rate, the change can only turn a no_media into alive; ti_21's chat-level bytes stay
    no_media against its playing rate."""
    srx = [(0.0, 0), (60.0, int(1729.4 * 1000 / 8 * 60))]
    assert B.dead_window(srx, 3636)["stream"] == "no_media"                   # the recorded misread
    dw = B.dead_window(srx, 3636, playing_kbps=2000.0)
    assert dw["stream"] == "alive" and dw["reference_kbit"] == 2000.0 and dw["share_of_shaper"] == 0.476, dw
    ti21 = [(0.0, 0), (60.0, int(122 * 1000 / 8 * 60))]
    assert B.dead_window(ti21, 952, playing_kbps=900.0)["stream"] == "no_media"
    assert B.dead_window(ti21, 952, playing_kbps=None)["reference_kbit"] == 952
    # the playing rate: the shaped downstream over the 30 s before the dead clock, only while the player progressed
    w = B.TTRoomWatch()
    b, t = 0, 1000.0
    for k in range(40):                                                       # 40 s playing at 2000 kbit/s
        b += int(2000 * 1000 / 8)
        w.feed({"has_video": True, "src": True, "rs": 4, "ct": float(k), "buf_end": k + 3.0, "decoded": None}, t,
               shaped_rx_bytes=b)
        t += 1.0
    dead_at = t
    for k in range(5):                                                        # then rs 0, the stream still arriving
        b += int(1700 * 1000 / 8)
        w.feed({"has_video": True, "src": True, "rs": 0, "ct": 0.0, "buf_end": None, "decoded": None}, t,
               shaped_rx_bytes=b)
        t += 1.0
    assert 1980 <= w.playing_kbps(dead_at) <= 2000, w.playing_kbps(dead_at)   # the dead tick's own second: 1700
    assert w.playing_kbps(dead_at + 30) is None                              # mostly not progressing: no reference
    assert B.TTRoomWatch().playing_kbps(dead_at) is None and w.playing_kbps(None) is None


def test_media_listener_records():
    """The tt media-internals recorder (supervisor 2026-10-18): Media-domain properties per player, events / errors and
    error / warning / info messages are written; debug messages and other domains are not; snapshot() for the dead record."""
    m = B.MediaListener("ws://unused", None)
    assert m.handle({"method": "Network.dataReceived", "params": {}}, 1.0) is None
    r = m.handle({"method": "Media.playerPropertiesChanged", "params": {"playerId": "p1", "properties": [
        {"name": "kVideoDecoderName", "value": "VaapiVideoDecoder"}, {"name": "kIsPlatformVideoDecoder", "value": "true"}]}}, 2.0)
    assert r["method"] == "playerPropertiesChanged" and r["t"] == 2.0
    assert m.handle({"method": "Media.playerMessagesLogged", "params": {"playerId": "p1", "messages": [
        {"level": "debug", "message": "x"}]}}, 3.0) is None
    r = m.handle({"method": "Media.playerMessagesLogged", "params": {"playerId": "p1", "messages": [
        {"level": "debug", "message": "x"}, {"level": "error", "message": "VaapiVideoDecoder: decode failed"}]}}, 4.0)
    assert [x["level"] for x in r["messages"]] == ["error"]
    m.handle({"method": "Media.playerErrorsRaised", "params": {"playerId": "p1", "errors": [
        {"errorType": "PipelineStatus", "code": 3}]}}, 5.0)
    snap = m.snapshot()
    assert snap["players"]["p1"]["kVideoDecoderName"] == "VaapiVideoDecoder"
    assert [x["method"] for x in snap["recent"]] == ["playerMessagesLogged", "playerErrorsRaised"]
    for k in range(B.MEDIA_RECENT_N + 5):
        m.handle({"method": "Media.playerEventsAdded", "params": {"playerId": "p1", "events": [{"timestamp": k}]}}, 6.0 + k)
    assert len(m.snapshot()["recent"]) == B.MEDIA_RECENT_N
    # decision A item 3 (every platform): summary() = meta media_decoders; each player's load kind (a MediaStream
    # player's kLoad names a stream id: "stream"), and the players that named a video decoder with codec / resolution
    m = B.MediaListener("ws://unused", None)
    for pid, url in (("p1", "blob:https://www.tiktok.com/x"), ("p2", "ea0cb5a7-72bd-4095-968a-47a0db0aa483"),
                     ("p3", "data:audio/mpeg;base64,AA")):
        m.handle({"method": "Media.playerEventsAdded", "params": {"playerId": pid, "events": [
            {"timestamp": 1, "value": json.dumps({"event": "kWebMediaPlayerCreated"})},
            {"timestamp": 2, "value": json.dumps({"event": "kLoad", "url": url})}]}}, 10.0)
    m.handle({"method": "Media.playerPropertiesChanged", "params": {"playerId": "p1", "properties": [
        {"name": "kVideoDecoderName", "value": "VaapiVideoDecoder"}, {"name": "kIsPlatformVideoDecoder", "value": "true"},
        {"name": "kVideoTracks", "value": json.dumps([{"codec": "h264"}])}, {"name": "kResolution", "value": "1440x1280"}]}}, 11.0)
    s = m.summary()
    assert s["players"] == 3 and s["loads"] == {"blob": 1, "stream": 1, "data": 1}, s
    assert s["video_decoders"] == [{"player": "p1", "load": "blob", "t_load": 10.0, "video_decoder": "VaapiVideoDecoder",
                                    "platform_video_decoder": "true", "codec": "h264", "resolution": "1440x1280"}], s
    assert B._media_load_kind("not json") is None and B._media_load_kind(json.dumps({"event": "kPlay"})) is None


def test_media_snapshot_is_capped(monkeypatch):
    """levi's audit of 1bcdc5e: the dead-trigger snapshot runs before the re-entry, so it is capped at
    MEDIA_SNAPSHOT_MAX_S and records snapshot_s; a slow page read leaves timed_out true and keeps what finished."""
    import time as _t

    class Page:
        def __init__(self, delay):
            self.delay = delay

        def eval(self, js):
            _t.sleep(self.delay)
            return [{"rs": 0, "error": {"code": 3, "message": "PIPELINE_ERROR_DECODE"}}]
    monkeypatch.setattr(B, "_browser_call", lambda *a, **k: {"gpu": {"featureStatus": {"video_decode": "enabled"}}})
    b = B.BrowserSession.__new__(B.BrowserSession)
    b._media, b._page = B.MediaListener("ws://unused", None), Page(0.0)
    snap = b._media_snapshot()
    assert not snap["timed_out"] and snap["videos"][0]["error"]["code"] == 3 and snap["gpu"]["video_decode"] == "enabled"
    assert snap["snapshot_s"] < 1.0 and snap["media_events"]["players"] == {}
    b._page = Page(B.MEDIA_SNAPSHOT_MAX_S + 1.5)
    t0 = _t.time()
    snap = b._media_snapshot()
    assert snap["timed_out"] and "videos" not in snap and _t.time() - t0 < B.MEDIA_SNAPSHOT_MAX_S + 0.5, snap

def test_class_switches_and_boundaries():
    """levi's FROZEN_COLLECTOR audit (2026-10-18). F1: a recovered dead run and a recovered frozen run each have their own
    class switch (RECOVERED_DEAD_CLASS / RECOVERED_FROZEN_CLASS, both "normal"); flipping one moves only its own kind.
    F2 (Q am. 12): dead fails at >= T, frozen above T; both sides pinned, in session_class and in check_session's verdict
    (both use entries.dead_over_t / frozen_over_t)."""
    C = E.session_class
    for kw in ({"recovered_dead": True}, {"recovered_frozen": True}):
        assert C("twitch", "PASS", None, [], None, None, 10, 660, **kw) == "normal"
    old = E.RECOVERED_DEAD_CLASS, E.RECOVERED_FROZEN_CLASS
    try:
        E.RECOVERED_DEAD_CLASS = "player_dead_recovered"
        assert C("tiktok", "PASS", None, [], 10, 60, 10, 720, recovered_dead=True) == "player_dead_recovered"
        assert C("twitch", "PASS", None, [], None, None, 10, 660, recovered_frozen=True) == "normal"
        E.RECOVERED_DEAD_CLASS = old[0]
        E.RECOVERED_FROZEN_CLASS = "player_frozen_recovered"
        assert C("twitch", "PASS", None, [], None, None, 10, 660, recovered_frozen=True) == "player_frozen_recovered"
        assert C("tiktok", "PASS", None, [], 10, 60, 10, 720, recovered_dead=True) == "normal"
        assert list(E.CLASS_PRECEDENCE).index("player_frozen_recovered") == list(E.CLASS_PRECEDENCE).index("player_frozen") + 1
    finally:
        E.RECOVERED_DEAD_CLASS, E.RECOVERED_FROZEN_CLASS = old
    T, TICK = 60, 1.0
    assert not E.dead_over_t(T - TICK, T) and E.dead_over_t(T, T)                    # dead: T - 1 tick passes, T fails
    assert C("tiktok", "PASS", None, [], T - TICK, T, 0, 720) == "normal" and C("tiktok", "PASS", None, [], T, T, 0, 720) == "player_dead"
    F = 660
    assert not E.frozen_over_t(F, F) and E.frozen_over_t(F + TICK, F)                # frozen: T passes, T + 1 tick fails
    assert C("twitch", "PASS", None, [], None, None, F, F) == "normal" and C("twitch", "PASS", None, [], None, None, F + TICK, F) == "player_frozen"
    assert not E.dead_over_t(None, T) and not E.frozen_over_t(F, None)
    import check_session as CS                                                       # the verdict, the same boundaries
    net = {"pcap": True, "packets": 1, "ssl_keys": True}

    def why(**lab):
        base = {"n_res_meta": 100, "tick_presence": 1.0, "v2_columns": True, "frozen_playhead_T_s": F, "dead_player_T_s": T}
        return " | ".join(CS.verdict({**base, **lab}, net, {}, [], 0.0)[1])
    assert "player_dead" not in why(dead_player_max_s=T - TICK) and "player_dead" in why(dead_player_max_s=T)
    assert "player_frozen" not in why(frozen_playhead_max_s=F) and "player_frozen" in why(frozen_playhead_max_s=F + TICK)


def test_one_reentry_then_the_session_ends():
    """supervisor ruling 2026-10-18: one re-entry of the same room on player_dead, then a death ends the session; the
    re-entry's outcome: playing -> continue, the ended banner -> room_ended (the host ended), else player_dead."""
    assert [B.dead_action(n) for n in (0, 1, 2)] == ["reenter", "abort", "abort"]
    assert B.reentry_outcome(True, {}) == "continue"
    assert B.reentry_outcome(False, {"dead": "ended_page"}) == "room_ended"
    assert B.reentry_outcome(False, {"ended_text": True, "dead": "not_playing"}) == "room_ended"
    assert B.reentry_outcome(False, {"dead": "not_playing"}) == "player_dead"
    assert B.reentry_outcome(False, None) == "player_dead"


def test_reenter_room_records_an_entry(monkeypatch):
    class Page:
        def __init__(self):
            self.navigated = []
        def navigate(self, url):
            self.navigated.append(url)
        def eval(self, js):
            return self.navigated[-1] if "location.href" in js else None
    for confirm, outcome in (((True, {"rs": 4, "ct": 3.0}), "continue"), ((False, {"dead": "ended_page", "ended_text": True}), "room_ended"),
                             ((False, {"dead": "not_playing", "rs": 0}), "player_dead")):
        b = B.BrowserSession("tiktok")
        b._page = Page()
        monkeypatch.setattr(B, "_wait_for_load", lambda page, timeout=15: None)
        seen = []
        monkeypatch.setattr(B, "_tt_confirm_playing", lambda page, seconds, trace=None, c=confirm: seen.append(seconds) or c)
        dw = {"shaped_rx_kbps": 1379.0, "shaper_kbit": 1459, "share_of_shaper": 0.945, "stream": "alive"}
        r = b._reenter_room({"room": "https://www.tiktok.com/@room_b/live", "dead_s": 60.5, "dead_window": dw})
        assert r["outcome"] == outcome and r["reason"] == "player_dead" and r["dead_window"] == dw, r
        assert b._page.navigated == ["https://www.tiktok.com/@room_b/live"]
        assert b.room_rotation["rooms"][-1] is r and b.player_dead_reentries == [r]
        assert r["t_enter"] >= r["t_nav"] and (r["t_playing"] is not None) == (outcome == "continue")
        assert seen == [B.TT_REENTRY_CONFIRM_S] == [60] and r["confirm_s"] == 60


def test_a_player_dead_reentry_is_an_entry():
    nl = [{"timestamp": 100.0, "speed": 1459, "state": "6"}]
    rows = [{"type": "start", "ts": "0"}]
    rows += [{"type": "res_meta", "ts": str(i * 1000), "current_time": str(i), "url": "a", "video_w": "720"} for i in range(0, 80)]
    rows += [{"type": "res_meta", "ts": str(i * 1000), "current_time": str(i - 150), "url": "a", "video_w": "720"} for i in range(150, 200)]
    dw = {"stream": "alive", "share_of_shaper": 0.945}
    meta = {"channel_url": "a", "room_rotation": {"rooms": [{"room": "a", "first": True},
            {"room": "a", "t_nav": 145.0, "t_enter": 148.0, "ok": True, "reason": "player_dead", "outcome": "continue", "dead_window": dw}]}}
    ents = E.compute_entries(rows, meta, nl)
    re_ = [e for e in ents if e["kind"] == "room_reentry"]
    assert len(re_) == 1 and re_[0]["reason"] == "player_dead" and re_[0]["dead_window"] == dw and re_[0]["outcome"] == "continue"
    assert re_[0]["shaper_at_entry"]["state"] == "6" and re_[0]["sustained_60s"]


def test_ti33_reentry_window(monkeypatch):
    """chaos ti_33 (2026-10-18, fixture): after the re-entry the room page loaded in about 8 s (video_start) and the new
    player sat at readyState 0 until the 15 s window ended it (abort 28 s after the navigation; the shots: tt's loader
    on a blank page). The real confirm loop, on a fake clock and page: the ti_33 timeline (no data at all) still ends as
    player_dead within the 60 s window; a room whose data arrives at 40 s now continues; loading samples are counted."""
    fx = json.loads((FX / "tt_ti33_reentry_2026-10-18.json").read_text())
    rows = [dict(zip(fx["columns"], r)) for r in fx["rows"]]
    tn = fx["reentry"]["t_nav"]
    vs = min(float(r["ts"]) / 1000 - tn for r in rows if r["type"] == "video_start" and float(r["ts"]) / 1000 > tn)
    assert 5 <= vs <= 12 and fx["reentry"]["outcome"] == "player_dead" and fx["reentry"]["confirm"]["rs"] == 0
    assert all(r["ready_state"] == "0" for r in rows if r["type"] == "res_meta" and float(r["ts"]) / 1000 > tn)

    class Clock:
        def __init__(self):
            self.t = 1000.0
        def time(self):
            return self.t
        def sleep(self, dt):
            self.t += dt

    def run(data_at):
        clock = Clock()
        t0 = clock.t

        class Page:
            def navigate(self, url):
                pass
            def eval(self, js):
                if js == B._TT_PLAYER_STATE_JS:
                    el = clock.t - t0
                    if el < vs:                                      # the page still loading: no player yet
                        return json.dumps({"has_video": False, "loading": True})
                    if data_at is None or el < data_at:              # the new player without data
                        return json.dumps({"has_video": True, "src": True, "rs": 0, "ct": 0, "buf_end": None,
                                           "decoded": None, "paused": True, "loading": True, "ended_text": False})
                    ct = el - data_at
                    return json.dumps({"has_video": True, "src": True, "rs": 4, "ct": ct, "buf_end": ct + 3,
                                       "decoded": int(ct * 1e4), "paused": False, "loading": False, "ended_text": False})
                return "https://www.tiktok.com/@room_c/live" if "location.href" in js else None
        monkeypatch.setattr(B, "time", clock)
        monkeypatch.setattr(B, "_wait_for_load", lambda page, timeout=15: None)
        b = B.BrowserSession("tiktok")
        b._page = Page()
        return b._reenter_room({"room": "https://www.tiktok.com/@room_c/live", "dead_s": 60.4,
                                "dead_window": fx["reentry"]["dead_window"]})
    r = run(None)                                    # ti_33 as recorded: no data at all
    assert r["outcome"] == "player_dead" and r["s_to_data"] is None and r["samples_loading"] > 0, r
    r = run(40)                                      # a slow room: data at 40 s (beyond the old 15 s window)
    assert r["outcome"] == "continue" and 39 <= r["s_to_data"] <= 42, r


class _Patch:
    """A minimal monkeypatch for running the tests as a script (apply_at_boundary.sh runs each test_*.py with python)."""
    def __init__(self):
        self.undo = []
    def setattr(self, obj, name, value):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)
    def close(self):
        for obj, name, value in reversed(self.undo):
            setattr(obj, name, value)


def test_ti37_recovered_reads_normal():
    """levi 2026-10-18 ruling (a), chaos ti_37 (fixture: labels from the start to 150 s after the re-entry, net_labels,
    the re-entry record): the one long dead run holds the outcome-continue re-entry's t_nav, so it is reported as
    recovered_dead_s and is not the player_dead verdict / class; a second long dead run, recovered or not, is."""
    import check_session as CS
    fx = json.loads((FX / "tt_ti37_recovered_2026-10-18.json").read_text())
    rows = [dict(zip(fx["columns"], r)) for r in fx["rows"]]
    re_ = fx["player_dead_reentries"]
    T = CS.PLAYER_DEAD_MAX_S["tiktok"]
    d = E.dead_player(rows, re_)
    assert d["recovered_runs"] == 1 and 69 <= d["recovered"]["recovered_dead_s"] <= 70, d
    assert d["max_all_s"] >= T > d["max_s"], d                      # before the ruling: player_dead by construction
    lab = CS.label_checks(rows, "tiktok", re_)
    assert lab["dead_player_max_s"] < T and lab["recovered_dead"]["window"][0] < re_[0]["t_nav"] < lab["recovered_dead"]["window"][1]
    ents = E.compute_entries(rows, {"platform": "tiktok", "room_rotation": fx["room_rotation"],
                                    "channel_url": "https://www.tiktok.com/@room/live"}, fx["net_labels"])
    assert [(e["kind"], e.get("reason"), e["shaper_at_entry"]["state"]) for e in ents] == \
        [("stream", None, "unshaped"), ("room_reentry", "player_dead", "3")], ents   # (3): a shaped entry, counted
    assert E.startup_rates(ents)["room_reentry:3"] == {"entries": 1, "failed": 0, "rate": 0.0}
    cls = lambda dd, v="PASS": E.session_class("tiktok", v, None, ents, dd["max_s"], T, 0, 900, recovered_dead=bool(dd["recovered"]))
    assert cls(d) == "normal"
    # a second long dead run 300 s later, not recovered -> player_dead (one recovery per session)
    t_shift = 300_000
    run = [r for r in rows if r["type"] == "res_meta" and d["recovered"]["window"][0] <= float(r["ts"]) / 1000 <= d["recovered"]["window"][1]]
    tail = [dict(r, ts=str(float(r["ts"]) + t_shift)) for r in run] + [dict(run[-1], ts=str(float(run[-1]["ts"]) + t_shift + 1000), ready_state="4")]
    d2 = E.dead_player(rows + tail, re_)
    assert d2["max_s"] >= T and cls(d2) == "player_dead", d2
    # a second continue re-entry inside that run too: two recovered runs -> nothing excluded -> player_dead
    re2 = re_ + [dict(re_[0], t_nav=re_[0]["t_nav"] + t_shift / 1000)]
    d3 = E.dead_player(rows + tail, re2)
    assert d3["recovered_runs"] == 2 and d3["recovered"] is None and cls(d3) == "player_dead", d3
    # no continue outcome (the re-entry died too) -> player_dead as before
    d4 = E.dead_player(rows, [dict(re_[0], outcome="player_dead")])
    assert d4["recovered"] is None and cls(d4) == "player_dead"
    # option (b): the constant flips the class, kept and not counted
    old = E.RECOVERED_DEAD_CLASS
    try:
        E.RECOVERED_DEAD_CLASS = "player_dead_recovered"
        assert cls(d) == "player_dead_recovered" and cls(d2) == "player_dead"
    finally:
        E.RECOVERED_DEAD_CLASS = old


def main() -> int:
    test_ti20_replay()
    test_low_state_hold()
    test_room_ended_carries_the_reentry_confirm()
    test_preroll_ad_sustains_the_entry()
    test_recovery_is_a_discontinuity()
    test_entries_and_class()
    test_precedence()
    test_rates_and_reentry()
    test_tt_start_failure_entry()
    test_ti32_dead_after_end_with_the_stream_alive()
    test_ti43_dead_window_reference()
    test_media_listener_records()
    test_class_switches_and_boundaries()
    test_one_reentry_then_the_session_ends()
    for t in (test_reenter_room_records_an_entry, test_ti33_reentry_window, test_media_snapshot_is_capped):
        mp = _Patch()
        try:
            t(mp)
        finally:
            mp.close()
    test_a_player_dead_reentry_is_an_entry()
    test_ti37_recovered_reads_normal()
    print("test_player_dead: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
