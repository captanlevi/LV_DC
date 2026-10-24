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



def test_open_clock_closed_on_rebuild_and_end():
    """levi 03:51 (ti_100084: two dead-clock entries read end None): a running clock's entry is closed "replaced" when the
    loop rebuilds the watch (room grace / re-entry) and "session_end" at the loop's exit; reset and fired as before;
    the rebuilt watch shares the log and starts its own entry."""
    dead = {"has_video": True, "src": True, "rs": 0, "ct": 0, "buf_end": None, "decoded": None, "stall_label": True}
    log = {"low_spans": [], "dead_clocks": []}
    w = B.TTRoomWatch(log=log)
    for i in range(30):                                                  # 30 s dead at state 7: a clock runs
        assert w.feed(dead, 1000.0 + i, shaper_state=7) is None
    w.close_open(1030.0, "replaced")
    assert log["dead_clocks"] == [{"start": 1001.0, "end": 1030.0, "how": "replaced"}], log
    w.close_open(1031.0, "replaced")                                     # nothing running: no change
    assert len(log["dead_clocks"]) == 1
    w = B.TTRoomWatch(log=log)
    for i in range(10):
        w.feed(dead, 2000.0 + i, shaper_state=7)
    w.close_open(2010.0, "session_end")
    assert log["dead_clocks"][-1] == {"start": 2001.0, "end": 2010.0, "how": "session_end"}, log
    assert all(c["end"] is not None for c in log["dead_clocks"])
    src = (HERE / "browser.py").read_text()
    loop = src[src.index("def _room_watch_loop"):src.index("def _media_snapshot")]
    assert loop.count('w.close_open(time.time(), "replaced")') == 2 and loop.count('w.close_open(time.time(), "session_end")') == 2

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


def test_held_dead_measure_and_watch_log():
    """end-of-day-2 tip (levi 09:40 / 11:22): with a tt session's net labels the player_dead measure leaves out what
    the collector's low-state hold held (states 1-4 and 30 s after); without them it is dead_runs' as before; the
    TTRoomWatch records its low spans and dead clocks (meta.json player_dead_watch) for the reader to match."""
    def rows(dead_from, dead_to, total=900, t0=1000.0):
        out = []
        for k in range(total):
            dead = dead_from <= k < dead_to
            out.append({"ts": str((t0 + k) * 1000), "type": "res_meta", "current_time": str(k), "is_ad": "False",
                        "paused": "False", "ready_state": "0" if dead else "4", "video_w": "720"})
        return out
    net = [{"timestamp": 1000.0, "state": "8", "speed": 2000}, {"timestamp": 1300.0, "state": "2", "speed": 300},
           {"timestamp": 1500.0, "state": "7", "speed": 1500}]
    r = rows(380, 500)
    assert E.dead_player(r, None)["max_s"] == 119.0                          # as before: no net labels
    assert E.dead_player(r, None, net)["max_s"] == 0.0                       # all held: state 2
    r2 = rows(450, 620)                                                      # held to 1530, then 1531 .. 1619 = 88 s
    assert E.held_dead_runs(r2, net) == [(1531.0, 1619.0)] and E.dead_player(r2, None, net)["max_s"] == 88.0
    assert E.held_dead_runs(r, [{"timestamp": 1000.0, "state": "8"}]) == E.dead_runs(r)   # no low state: unchanged
    log = {"low_spans": [], "dead_clocks": []}
    w = B.TTRoomWatch(log=log)
    dead = {"has_video": True, "src": True, "rs": 0, "ct": 0, "buf_end": None, "decoded": None, "stall_label": True}
    for i in range(20):
        w.feed(dead, 2000.0 + i, shaper_state=2)                             # held: no clock, one low span
    fired = None
    for i in range(20, 200):
        fired = fired or w.feed(dead, 2000.0 + i, shaper_state=7)
    assert fired == "player_dead"
    assert log["low_spans"] == [[2000.0, 2019.0]], log
    assert log["dead_clocks"] == [{"start": 2050.0, "end": 2110.0, "how": "fired"}], log   # grace to 2049, T 60


def test_check_defers_to_the_fired_clock():
    """levi 12:04: with player_dead_watch on tt, dead_over_t only on a fired clock or a run open at the last tick; a held
    run >= T by the labels alone is not player_dead, the disagreement flagged with its margin; no watch: as before."""
    import check_session as CS

    def rows(dead_from, dead_to, total=900, t0=1000.0):
        return [{"ts": str((t0 + k) * 1000), "type": "res_meta", "current_time": str(k), "is_ad": "False",
                 "paused": "False", "ready_state": "0" if dead_from <= k < dead_to else "4", "video_w": "720"}
                for k in range(total)]
    r = rows(450, 620)
    assert not E.dead_open_at_end(r) and E.dead_open_at_end(rows(800, 900)) and not E.dead_open_at_end(rows(0, 0))

    def run(rs, pdw, platform="tiktok"):
        lab = {"dead_player_max_s": 88.0, "dead_player_T_s": 60.0}
        CS.dead_clock_of_record(lab, rs, platform, pdw)
        return lab
    lab = run(r, {"low_spans": [[1300.0, 1499.0]], "dead_clocks": [{"start": 1531.0, "end": 1540.0, "how": "reset"}]})
    assert lab["dead_player_class_s"] == 0.0 and not E.dead_over_t(lab["dead_player_class_s"], 60.0)
    assert lab["dead_clock_of_record"] == {"watch": True, "fired": False, "open_at_end": False, "open_run_held_s": None,
                                           "disagrees": True, "margin_s": 28.0, "provisional": None,
                                           "unresolved_clocks": 0}
    lab = run(r, {"low_spans": [], "dead_clocks": [{"start": 1531.0, "end": 1591.0, "how": "fired"}]})
    assert lab["dead_player_class_s"] == 88.0 and not lab["dead_clock_of_record"]["disagrees"]
    lab = run(rows(800, 900), {"low_spans": [], "dead_clocks": []})    # open at the end and itself >= T: its own length
    assert lab["dead_player_class_s"] == 99.0 and lab["dead_clock_of_record"]["open_at_end"]
    for pdw, plat in ((None, "tiktok"), ({"dead_clocks": []}, "youtube")):          # older sessions / not tt: as before
        lab = run(r, pdw, plat)
        assert lab["dead_player_class_s"] == 88.0 and lab["dead_clock_of_record"] is None


def test_open_run_is_measured_on_its_own():
    """levi 03:51 / 03:58 / 04:06 (H), on ti_100084's real timeline (shagun 04:06, t+ from started_at): readyState 0 from
    t+121 to t+262; a state-1 span t+88 -> t+148 (+ 30 s grace to t+178, where the collector's clock started and was
    left unresolved: its watch was rebuilt); then play; then a ~5 s dead run open at the room end, also unresolved.
    The class: the open run counts on its own held length (5 s < T): 0, the disagreement recorded, provisional. The mask
    export: each unresolved clock's run as its HELD parts (t+179 -> t+262 here, not the part inside the span), with the
    raw run beside it; times in epoch seconds. A raw run itself >= T with no low span: player_dead, not exported."""
    import check_session as CS
    t0 = 1_792_000_000.0

    def rows(spans, total=800):
        return [{"ts": str((t0 + k) * 1000), "type": "res_meta", "current_time": str(k), "is_ad": "False",
                 "paused": "False", "ready_state": "0" if any(a <= k < b for a, b in spans) else "4", "video_w": "720"}
                for k in range(total)]
    labels = [{"timestamp": t0 + 88, "speed": 300, "state": "1"}, {"timestamp": t0 + 148, "speed": 900, "state": "5"}]
    pdw = {"low_spans": [[t0 + 88, t0 + 148]],
           "dead_clocks": [{"start": t0 + 178, "end": None, "how": None}, {"start": t0 + 795, "end": None, "how": None}]}
    r = rows([(121, 263), (795, 800)])
    lab = {"dead_player_max_s": 83.0, "dead_player_T_s": 60.0}
    CS.dead_clock_of_record(lab, r, "tiktok", pdw, labels)
    d = lab["dead_clock_of_record"]
    assert lab["dead_player_class_s"] == 0.0 and d["open_at_end"] and d["open_run_held_s"] == 4.0, d
    assert d["disagrees"] and d["margin_s"] == 23.0 and d["provisional"] == "held clock unknown" and d["unresolved_clocks"] == 2
    assert lab["provisional_runs"] == [
        {"onset": t0 + 179, "end": t0 + 262, "held_s": 83.0, "raw_onset": t0 + 121, "raw_end": t0 + 262, "raw_s": 141.0,
         "low_span": True},
        {"onset": t0 + 795, "end": None, "held_s": 4.0, "raw_onset": t0 + 795, "raw_end": None, "raw_s": 4.0,
         "low_span": False}], lab["provisional_runs"]
    import stuck as S
    assert all(t0 + 148 + S.LOW_HOLD_GRACE_S < x["onset"] for x in lab["provisional_runs"])   # nothing inside the hold
    # levi 04:00 (1): the provisional session is counted: no player_dead reason, PASS / normal, the flag beside it
    net = {"pcap": True, "packets": 1, "ssl_keys": True}
    base = {"n_res_meta": 100, "tick_presence": 1.0, "v2_columns": True, "dead_player_T_s": 60.0}
    v, why = CS.verdict({**base, **lab}, net, {}, [], 0.0)
    assert v == "PASS" and not any("player_dead" in w for w in why), why
    assert E.session_class("tiktok", v, None, [], lab["dead_player_class_s"], 60.0, None, None) == "normal"
    # (2) the exception: an unresolved raw run >= T with no low span inside reads player_dead, nothing exported
    hard = {"dead_player_max_s": 86.0, "dead_player_T_s": 60.0}
    CS.dead_clock_of_record(hard, rows([(500, 586)]), "tiktok",
                            {"low_spans": [[t0 + 88, t0 + 148]], "dead_clocks": [{"start": t0 + 500, "end": None, "how": None}]},
                            labels)
    assert hard["dead_player_class_s"] == 85.0 and hard["provisional_runs"] == [] and hard["dead_clock_of_record"]["provisional"] is None
    v, why = CS.verdict({**base, **hard}, net, {}, [], 0.0)
    assert v == "FAIL" and any("player_dead" in w for w in why)
    # ... and the same >= T run with a low span inside it is provisional (its held parts exported)
    near_labels = [{"timestamp": t0 + 480, "speed": 300, "state": "2"}, {"timestamp": t0 + 520, "speed": 900, "state": "6"}]
    near = {"dead_player_max_s": 36.0, "dead_player_T_s": 60.0}
    CS.dead_clock_of_record(near, rows([(500, 586)]), "tiktok",
                            {"low_spans": [[t0 + 480, t0 + 520]], "dead_clocks": [{"start": t0 + 550, "end": None, "how": None}]},
                            near_labels)
    assert near["dead_player_class_s"] == 0.0 and [(x["onset"], x["end"]) for x in near["provisional_runs"]] == [(t0 + 551, t0 + 585)]
    # an open run itself >= T still classes player_dead; an open run inside the hold counts 0
    lab = {"dead_player_max_s": 85.0, "dead_player_T_s": 60.0}
    CS.dead_clock_of_record(lab, rows([(700, 800)]), "tiktok", {"low_spans": [], "dead_clocks": []}, labels)
    assert lab["dead_player_class_s"] == 99.0 and not lab["dead_clock_of_record"]["disagrees"]
    held = [{"timestamp": t0 + 690, "speed": 300, "state": "2"}]
    lab = {"dead_player_max_s": 85.0, "dead_player_T_s": 60.0}
    CS.dead_clock_of_record(lab, rows([(700, 800)]), "tiktok", {"low_spans": [], "dead_clocks": []}, held)
    assert lab["dead_clock_of_record"]["open_run_held_s"] == 0.0 and lab["dead_player_class_s"] == 0.0
    src = (HERE / "check_session.py").read_text()          # labels_block passes the net labels to the clock of record
    assert "nl = E.read_net_labels(sd)" in src and \
        'dead_clock_of_record(lab, label_rows, platform, meta.get("player_dead_watch"), nl, navs, shared)' in src

def test_dead_runs_split_at_our_navigation():
    """levi 11:40 (peter's margin fact a5ffab57; ti_100086 / ti_100090): a dead run ends at our re-entry's navigation,
    where the collector's watch is rebuilt and its clock cut; the new page's load ticks after it are a run of their own.
    ti_100086 / ti_100090 rebuilt from check_Tstar_all (ef9905b22161): the held part ends at t_nav (59.485 / 59.986 s,
    < T: no fire, as the collector saw), not at the last load tick past it (63.776 / 63.307). A hard run (no low span,
    raw >= T) joined across a navigation, each part < T: not player_dead; a hard run >= T before the navigation: still."""
    import check_session as CS
    for onset, nav, last, held in ((1792597662.422, 1792597721.9073412, 1792597726.197, 59.485),
                                   (1792604608.539, 1792604668.5246737, 1792604671.847, 59.986)):
        raw0 = onset - 79.0                                          # raw - held = the low part + 30 s grace (levi)
        ticks = [raw0 - 20.0 + k for k in range(int(last - raw0 + 21))]
        rows = [{"ts": str(t * 1000), "type": "res_meta", "current_time": str(t - ticks[0]), "is_ad": "False",
                 "paused": "False", "ready_state": "0" if t >= raw0 else "4", "video_w": "720"} for t in ticks]
        labels = [{"timestamp": raw0 - 10.0, "speed": 300, "state": "2"},
                  {"timestamp": onset - 30.0 - 0.5, "speed": 1600, "state": "6"}]    # the hold ends 0.5 s before onset
        before = E.held_dead_runs(rows, labels)
        assert round(before[-1][1] - before[-1][0], 3) > 60.0, before          # unsplit: the load ticks join, > T
        parts = E.held_dead_runs(rows, labels, [nav])
        assert round(parts[0][0], 3) == onset and parts[0][1] == nav and round(nav - onset, 3) == held, parts
        assert parts[1][0] >= nav and parts[1][1] - parts[1][0] < 60.0, parts      # the load run, its own
        pdw = {"low_spans": [[raw0 - 10.0, onset - 30.5]], "dead_clocks": [{"start": onset - 0.5, "end": None, "how": None}]}
        lab = {"dead_player_max_s": held, "dead_player_T_s": 60.0}
        CS.dead_clock_of_record(lab, rows, "tiktok", pdw, labels, [nav])
        p0 = lab["provisional_runs"][0]
        assert lab["dead_player_class_s"] == 0.0 and (p0["onset"], p0["end"], p0["held_s"]) == (round(onset, 3), round(nav, 3), held), lab
        assert not lab["dead_clock_of_record"]["disagrees"], lab                  # held < T: the reader agrees
    assert E.nav_times({"frozen_reentries": [{"t_nav": 5.0}], "player_dead_reentries": [{"t_enter": 9.0}],
                        "room_rotation": {"rooms": [{"first": True, "t_nav": 1.0}, {"t_nav": 5.0}, {"room": "x"}]}}) == [5.0, 9.0]
    t0 = 1_792_000_000.0

    def rows(spans, total=800):
        return [{"ts": str((t0 + k) * 1000), "type": "res_meta", "current_time": str(k), "is_ad": "False",
                 "paused": "False", "ready_state": "0" if any(a <= k < b for a, b in spans) else "4", "video_w": "720"}
                for k in range(total)]
    labels = [{"timestamp": t0 + 88, "speed": 300, "state": "1"}, {"timestamp": t0 + 148, "speed": 900, "state": "5"}]
    pdw = lambda s: {"low_spans": [[t0 + 88, t0 + 148]], "dead_clocks": [{"start": t0 + s, "end": None, "how": None}]}
    net = {"pcap": True, "packets": 1, "ssl_keys": True}
    base = {"n_res_meta": 100, "tick_presence": 1.0, "v2_columns": True, "dead_player_T_s": 60.0}
    joined = {"dead_player_max_s": 86.0, "dead_player_T_s": 60.0}        # 500 -> 586 across a navigation at 540
    CS.dead_clock_of_record(joined, rows([(500, 586)]), "tiktok", pdw(500), labels, [t0 + 540.5])
    v, why = CS.verdict({**base, **joined}, net, {}, [], 0.0)
    assert joined["dead_player_class_s"] < 60.0 and v == "PASS" and not any("player_dead" in w for w in why), (joined, why)
    assert E.dead_runs(rows([(500, 586)]), [t0 + 540.5]) == [(t0 + 500, t0 + 540.5), (t0 + 541, t0 + 585)]
    hard = {"dead_player_max_s": 86.0, "dead_player_T_s": 60.0}          # >= T before the navigation at 580
    CS.dead_clock_of_record(hard, rows([(500, 586)]), "tiktok", pdw(500), labels, [t0 + 580.5])
    v, why = CS.verdict({**base, **hard}, net, {}, [], 0.0)
    assert hard["dead_player_class_s"] == 80.5 and v == "FAIL" and any("player_dead" in w for w in why), hard
    assert E.dead_runs(rows([(500, 586)])) == E.dead_runs(rows([(500, 586)]), [t0 + 700.0]) == [(t0 + 500, t0 + 585)]
    # the recovery rule matches the run our re-entry ended, never the new page's load run after it
    re_ = [{"t_nav": t0 + 540.5, "outcome": "continue"}]
    dp = E.dead_player(rows([(500, 586)]), re_, None, [t0 + 540.5])
    assert dp["recovered_runs"] == 1 and dp["recovered"]["window"] == [t0 + 500, t0 + 540.5], dp


def test_shared_deadline_reentry_tag():
    """levi 14:41 (reader tag): a tt re-entry whose T2 deadline and dead-clock T fall within one watch poll of each
    other is shared_deadline_reentry. ti_100084's real timings (meta): T2 window from 1792595484.2211607, the dead clock
    from 1792595514.52 -> deadlines 574.221 / 574.520, 0.299 s apart: shared. A dead clock 5 s later: not shared; no
    dead clock: None; a player_dead re-entry's T2 deadline from its frozen run and the shaper's rise above the rung."""
    import check_session as CS
    t0 = 1792595000.0
    fr = {"t_nav": 1792595574.9155204, "outcome": "continue", "t2_window": [1792595484.2211607, 1792595574.9132693]}
    meta = {"platform": "tiktok", "frozen_reentries": [fr],
            "player_dead_watch": {"dead_clocks": [{"start": 1792595514.52, "end": None, "how": None}]}}
    (r,) = CS.shared_deadline_reentries(meta, [], [])
    assert r["kind"] == "player_frozen" and r["diff_s"] == -0.299 and r["shared_deadline_reentry"] is True, r
    meta["player_dead_watch"]["dead_clocks"][0]["start"] += 5.0
    assert CS.shared_deadline_reentries(meta, [], [])[0]["shared_deadline_reentry"] is False
    meta["player_dead_watch"]["dead_clocks"] = [{"start": 1792595300.0, "end": 1792595320.0, "how": "reset"}]
    assert CS.shared_deadline_reentries(meta, [], [])[0]["shared_deadline_reentry"] is None      # no clock at the nav
    assert CS.shared_deadline_reentries({**meta, "platform": "twitch"}, [], []) == []
    # a player_dead re-entry (the dead watch won): T2 from the frozen run and the shaper's rise above the rung
    rows = [{"ts": str((t0 + k) * 1000), "type": "stall" if k == 400 else "res_meta", "current_time": "5.0",
             "is_ad": "False", "paused": "False", "video_w": "0", "ready_state": "0"} for k in range(400, 600)]
    labels = [{"timestamp": t0 + 380, "speed": 300, "state": "2"}, {"timestamp": t0 + 484, "speed": 1600, "state": "6"}]
    dmeta = {"platform": "tiktok", "lowest_rung": {"kbit": 500.0, "source": "tt_room_sdk_params"},
             "player_dead_reentries": [{"t_nav": t0 + 575.0, "outcome": "player_dead"}],
             "player_dead_watch": {"dead_clocks": [{"start": t0 + 514.3, "end": t0 + 574.4, "how": "fired"}]}}
    (d,) = CS.shared_deadline_reentries(dmeta, rows, labels)
    assert d["kind"] == "player_dead" and d["t2_deadline"] == round(t0 + 484 + 90.0, 3), d
    assert d["dead_deadline"] == round(t0 + 574.3, 3) and d["shared_deadline_reentry"] is True and d["outcome"] == "player_dead", d


def test_shared_deadline_run_is_not_compared_with_t():
    """levi 14:43: a dead run our own re-entry ends at the shared deadline is not compared with T (censored at t_nav,
    its length reported); untagged, the same run keeps the T comparison. A pre-Y session (no watch log, bea39af): the
    dead deadline is max(run onset, the low stretch's end + grace) + T from net_labels (ti_100058's shape: one frozen
    re-entry, recovered, the held part a hair over T)."""
    import check_session as CS
    t0 = 1792500000.0

    def rows(dead_from, nav, play_again, total=700):
        out = []
        for k in range(total):
            t = t0 + k
            dead = dead_from <= k < play_again
            out.append({"ts": str(t * 1000), "type": "res_meta", "current_time": "5.0" if dead else str(k),
                        "is_ad": "False", "paused": "False", "ready_state": "0" if dead else "4", "video_w": "720"})
        return out
    nav = t0 + 574.4
    r = rows(470, 574.4, 580)
    labels = [{"timestamp": t0 + 380, "speed": 300, "state": "2"}, {"timestamp": t0 + 483.5, "speed": 1600, "state": "6"}]
    fr = {"t_nav": nav, "outcome": "continue", "t2_window": [t0 + 484.22, nav - 0.002]}
    meta = {"platform": "tiktok", "frozen_reentries": [fr], "lvdc_commit": {"sha": "bea39af06adbbd83bee9d56ce1b17fdc892a7365"}}   # no watch log: bea39af
    (tag,) = CS.shared_deadline_reentries(meta, r, labels)
    assert tag["dead_source"] == "net_labels, the hold (no watch log)" and tag["dead_deadline"] == round(t0 + 513.5 + 60, 3), tag
    assert tag["t2_deadline"] == round(t0 + 574.22, 3) and tag["shared_deadline_reentry"] is True, tag
    navs = E.nav_times(meta)
    dp = E.dead_player(r, [], labels, navs)                                       # untagged: held 514 -> 574.4 > T
    assert round(dp["max_s"], 3) == 60.4 and E.dead_over_t(dp["max_s"], 60.0), dp
    dp = E.dead_player(r, [], labels, navs, [nav])     # tagged: not compared with T (4 s: the new page's own load run)
    assert dp["max_s"] == 4.0 and dp["shared_deadline_runs"] == [{"onset": t0 + 514, "censored_end": round(nav, 3), "s": 60.4}], dp
    lab = {"dead_player_max_s": dp["max_s"], "dead_player_T_s": 60.0}
    CS.dead_clock_of_record(lab, r, "tiktok", None, labels, navs, [nav])
    net = {"pcap": True, "packets": 1, "ssl_keys": True}
    base = {"n_res_meta": 100, "tick_presence": 1.0, "v2_columns": True}
    v, why = CS.verdict({**base, **lab}, net, {}, [], 0.0)
    assert v == "PASS" and not any("player_dead" in w for w in why), why
    # an untagged hard run elsewhere in the session still reads player_dead
    r2 = rows(470, 574.4, 580)
    for k in range(600, 690):
        r2[k]["ready_state"], r2[k]["current_time"] = "0", "9.0"
    dp2 = E.dead_player(r2, [], labels, navs, [nav])
    assert dp2["max_s"] == 89.0 and E.dead_over_t(dp2["max_s"], 60.0), dp2
    # levi 14:57: before the hold (ef58732) the dead clock ran from the run's onset: deadline 470 + 60, T2 574.22: no tie
    pre = {**meta, "lvdc_commit": {"sha": "ef58732" + "0" * 33}}
    (pt,) = CS.shared_deadline_reentries(pre, r, labels)
    assert pt["dead_source"].startswith("the run's onset") and pt["dead_deadline"] == round(t0 + 530, 3), pt
    assert pt["shared_deadline_reentry"] is False, pt
    far = {**fr, "t2_window": [t0 + 490.0, nav]}                                  # T2 due 580: 6 s from the dead T
    assert CS.shared_deadline_reentries({**meta, "frozen_reentries": [far]}, r, labels)[0]["shared_deadline_reentry"] is False


def test_ti100058_shared_deadline():
    """levi 14:43 / 14:44, on ti_100058's real timings (bea39af, no watch log; LVQoE data/t58_register_artifacts/ti_100058,
    meta c809fefa46f0, net_labels 0715407a60f7): state 1 from 1792533814.567 to 1792533874.829 (state 5); readyState 0
    from 1792533851.922; one frozen re-entry, T2 window from 1792533874.829, t_nav 1792533964.992, recovered. Both
    deadlines are the low exit + 90 s (diff 0.0): tagged. Untagged, its held part (from the hold's end) reads 60.07 >= T
    (the labels-only player_dead before the tag); tagged, it is not compared with T, the length reported."""
    import check_session as CS
    low0, low1, onset, nav = 1792533814.56691, 1792533874.8294663, 1792533851.9217, 1792533964.9919012
    def mk(nav):
        ticks = [onset - 60 + k for k in range(60)] + [onset + k for k in range(int(nav - onset) + 1)] + \
                [nav + 0.6 + k for k in range(4)] + [nav + 5.0 + k for k in range(30)]
        return [{"ts": str(t * 1000), "type": "res_meta", "is_ad": "False", "paused": "False", "video_w": "720",
                 "current_time": "100.0" if onset <= t < nav + 4.7 else str(t - ticks[0]),
                 "ready_state": "0" if onset <= t < nav + 4.7 else "4"} for t in ticks]
    rows = mk(nav)
    labels = [{"timestamp": low0, "speed": 203, "state": "1"}, {"timestamp": low1, "speed": 1219, "state": "5"}]
    meta = {"platform": "tiktok", "lvdc_commit": {"sha": "bea39af06adbbd83bee9d56ce1b17fdc892a7365"},
            "frozen_reentries": [{"t_nav": nav, "outcome": "continue", "t2_window": [low1, 1792533964.9910736]}]}
    (tag,) = CS.shared_deadline_reentries(meta, rows, labels)
    assert tag["diff_s"] == 0.0 and tag["shared_deadline_reentry"] is True and tag["dead_source"].startswith("net_labels"), tag
    assert abs(tag["nav_after_deadline_s"] - 0.162) < 0.002, tag
    # levi 14:55: equal deadlines alone do not tag; a re-entry 30 s after the shared deadline (another cause) is not tagged
    late = {"platform": "tiktok", "lvdc_commit": {"sha": "bea39af06adbbd83bee9d56ce1b17fdc892a7365"}, "frozen_reentries": [{"t_nav": nav + 30.0, "outcome": "continue",
                                                        "t2_window": [low1, nav + 30.0]}]}
    (lt,) = CS.shared_deadline_reentries(late, mk(nav + 30.0), labels)
    assert lt["diff_s"] == 0.0 and lt["shared_deadline_reentry"] is False and abs(lt["nav_after_deadline_s"] - 30.162) < 0.002, lt
    import browser as B
    assert CS.SHARED_DEADLINE_SNAPSHOT_S == B.MEDIA_SNAPSHOT_MAX_S
    navs = E.nav_times(meta)
    untagged = E.dead_player(rows, [], labels, navs)
    assert round(untagged["max_s"], 2) == 60.07 and E.dead_over_t(untagged["max_s"], 60.0), untagged
    tagged = E.dead_player(rows, [], labels, navs, [nav])
    assert not E.dead_over_t(tagged["max_s"], 60.0) and tagged["shared_deadline_runs"][0]["s"] == 60.07, tagged


def test_reentry_load_is_not_a_dead_run():
    """levi 15:06: dead ticks in [t_nav, t_playing] of our navigations that came back (E.nav_loads) are the new page
    loading, excluded from the dead measure, reported as reentry_load_runs (the labeler censors the same span). On
    ti_100043's real timings (ef587323, a player_dead re-entry, continue): the run 1792511608.02 -> 1792511710.83, t_nav
    1792511668.56, t_playing 1792511713.96: the post-nav 41.81 s is load, the measure is the session's other runs as
    before the split. A failed re-entry (no t_playing) keeps its post-nav run (ti_100057)."""
    a, nav, b, tp = 1792511608.0226002, 1792511668.5605295, 1792511710.8312998, 1792511713.96
    ticks = [a - 30 + k for k in range(30)] + [a + k for k in range(int(b - a) + 1)] + [b + 1 + k for k in range(20)]
    rows = [{"ts": str(t * 1000), "type": "res_meta", "is_ad": "False", "paused": "False", "video_w": "720",
             "current_time": "50.0" if a <= t <= b + 0.01 else str(t - ticks[0]),
             "ready_state": "0" if a <= t <= b + 0.01 else "4"} for t in ticks]
    meta = {"player_dead_reentries": [{"t_nav": nav, "t_playing": tp, "outcome": "continue", "dead_s": 60.6}],
            "room_rotation": {"rooms": [{"first": True}, {"t_nav": nav, "t_playing": tp, "reason": "player_dead"}]}}
    assert E.nav_loads(meta) == [(nav, tp)]
    navs = E.nav_times(meta)
    split = E.dead_player(rows, meta["player_dead_reentries"], None, navs)
    assert 40.0 < split["max_s"] < 42.0, split                                       # the load run, exposed by the split
    dp = E.dead_player(rows, meta["player_dead_reentries"], None, navs, None, E.nav_loads(meta))
    assert dp["max_s"] == 0.0 and dp["recovered"]["window"] == [a, nav], dp
    assert len(dp["reentry_load_runs"]) == 1 and dp["reentry_load_runs"][0]["s"] == round(split["max_s"], 3), dp
    failed = {"player_dead_reentries": [{"t_nav": nav, "outcome": "player_dead"}]}    # no t_playing: no load span
    fd = E.dead_player(rows, failed["player_dead_reentries"], None, navs, None, E.nav_loads(failed))
    assert E.nav_loads(failed) == [] and fd["reentry_load_runs"] == [] and round(fd["max_s"], 1) == 60.5, fd


def test_tag_rule_per_collector():
    """levi 15:06: without a watch log the hold follows the list's TT_LOWSTATE_POST_FIX (mirrored); one case per side.
    Where the LVQoE repo is beside this one (LVQOE_REPO), the mirror equals the list's tuple."""
    import os
    import re as _re
    import check_session as CS
    t0 = 1792500000.0
    rows = [{"ts": str((t0 + k) * 1000), "type": "res_meta", "is_ad": "False", "paused": "False", "video_w": "720",
             "current_time": "5.0" if 470 <= k < 580 else str(k), "ready_state": "0" if 470 <= k < 580 else "4"}
            for k in range(400, 650)]
    labels = [{"timestamp": t0 + 380, "speed": 300, "state": "2"}, {"timestamp": t0 + 483.5, "speed": 1600, "state": "6"}]
    fr = {"t_nav": t0 + 574.4, "outcome": "continue", "t2_window": [t0 + 483.5, t0 + 574.4]}
    for sha, want in (("bea39af06adbbd83bee9d56ce1b17fdc892a7365", True), ("0309d76e2f31f627c531ec316119712f31aee1dd", True),
                      ("ef587323" + "0" * 32, False)):
        (t,) = CS.shared_deadline_reentries({"platform": "tiktok", "lvdc_commit": {"sha": sha}, "frozen_reentries": [fr]},
                                             rows, labels)
        assert t["shared_deadline_reentry"] is want, (sha, t)
    repo = os.environ.get("LVQOE_REPO")
    if repo and (Path(repo) / "scripts" / "checkpoint_session_list.py").exists():
        src = (Path(repo) / "scripts" / "checkpoint_session_list.py").read_text()
        m = _re.search(r"TT_LOWSTATE_POST_FIX[^=]*=\s*\((.*?)\n\)", src, _re.S)
        assert m and tuple(_re.findall(r'"([0-9a-f]{40})"', m.group(1))) == CS.TT_LOWSTATE_POST_FIX, m and m.group(1)


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
    assert C("youtube", "FAIL", "stream_ended", ok, None, None, None, None) == "stream_ended"   # bundle item 3
    assert C("youtube", "FAIL", "stream_ended", bad, None, None, None, None) == "startup_failure"
    assert list(E.CLASS_PRECEDENCE) == ["not_live", "login_wall", "interrupted", "startup_failure", "room_ended",
                                        "stream_ended", "player_dead", "player_dead_recovered", "player_frozen",
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
    test_held_dead_measure_and_watch_log()
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
    test_dead_runs_split_at_our_navigation()
    test_shared_deadline_reentry_tag()
    test_shared_deadline_run_is_not_compared_with_t()
    test_ti100058_shared_deadline()
    test_reentry_load_is_not_a_dead_run()
    test_tag_rule_per_collector()
    test_open_clock_closed_on_rebuild_and_end()     # audit B3 (2026-10-24): never run as a script
    test_check_defers_to_the_fired_clock()
    test_open_run_is_measured_on_its_own()
    print("test_player_dead: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
