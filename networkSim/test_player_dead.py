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
    assert fired and fired[0] == "player_dead" and 170 <= fired[1] <= 174, fired     # dead run from 111.5 s
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
                                        "player_dead", "player_frozen", "qa_fail", "normal"]


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


def main() -> int:
    test_ti20_replay()
    test_recovery_is_a_discontinuity()
    test_entries_and_class()
    test_precedence()
    test_rates_and_reentry()
    test_tt_start_failure_entry()
    test_ti32_dead_after_end_with_the_stream_alive()
    print("test_player_dead: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
