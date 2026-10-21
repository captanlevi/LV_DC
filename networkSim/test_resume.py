#!/usr/bin/env python3
"""The collector presses play on a player that paused itself (levi 2026-10-17, chaos tw_10 / tw_12), and since levi's
ruling 00130 (2026-10-20, the twitch stall loops) only then: never in a stall, only on a self-pause with the buffer ahead
>= SELF_PAUSE_MIN_BUFFER_S held for SELF_PAUSE_HOLD_S, and RESUME_BACKOFF_S after the last restart.
browser.SelfPauseResumer decides; _PLAYER_STATE_JS and _PLAY_JS run under node with a stub DOM; tw_100010's replay
(fixtures/tw_100010_replay.json): none of the presses fall in its stall loops.

    python3 test_resume.py        (exit 0 = pass)
"""
import json
import shutil
import subprocess
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

STUB = r"""
const [stall, ad, paused, rs, ended, bufEnd] = JSON.parse(process.argv[2]);
let played = 0;
const small = {paused: false, readyState: 4, ended: false, clientWidth: 160, clientHeight: 90, getClientRects: () => [1],
               currentTime: 0, buffered: {length: 0}, play() { throw new Error('pressed the small video'); }};
const v = {paused, readyState: rs, ended, clientWidth: 800, clientHeight: 450, getClientRects: () => [1],
           currentTime: 100, buffered: {length: 2, start: (i) => [50, 99.5][i], end: (i) => [60, bufEnd][i]},
           play() { played++; return Promise.resolve(); }};
global.document = {documentElement: {dataset: {lvqoeStall: stall ? '1' : '0', lvqoeAd: ad ? '1' : '0'}},
                   querySelectorAll: () => [small, v]};
const r = eval(process.argv[1]);
console.log(JSON.stringify({r, played}));
"""


def js(src, stall=False, ad=False, paused=True, rs=4, ended=False, buf_end=102.0):
    out = subprocess.run([shutil.which("node"), "-e", STUB, src, json.dumps([stall, ad, paused, rs, ended, buf_end])],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def presses(states, r=None):
    """Run the resumer over (t, state) at the watcher's 1 Hz; the presses it makes."""
    r = r or B.SelfPauseResumer()
    return [(t, p) for t, st in states if (p := r.step(st, t))]


def S(paused=True, rs=4, stall=False, ad=False, ended=False, buf=2.0):
    return {"paused": paused, "rs": rs, "stall": stall, "ad": ad, "ended": ended, "buf": buf}


def test_pause_deadlock():
    """levi / supervisor 15:40 (the row's wording): one press when the paused polls are held for "buffer" (no stall
    label, readyState >= 3, under the floor, not an ad, not ended) with the buffer flat AND the playhead still for 30 s;
    still paused 30 s after it, the pause feeds the stuck-player rule as a frozen run from the pause's start. Fixtures:
    tw_100079's shape (held "buffer" for the whole pause) -> press; brad's twitch startup-rebuffer shape (readyState 2,
    buffered ahead ~0, held "stall", 62-172 s, resumes itself) -> no press."""
    assert (B.PAUSE_DEADLOCK_S, B.PAUSE_GROWTH_EPS_S) == (30.0, 0.1)

    def T(buf=0.4, ct=105.0, **k):
        return {**S(buf=buf, **k), "ct": ct}
    r = B.SelfPauseResumer()
    seq = [(t, T()) for t in range(100, 200)]                             # tw_100079: playhead ~105 s, buffer flat
    p = [(t, x) for t, st in seq if (x := r.step(st, t))]
    assert [(t, x["reason"], x["flat_s"]) for t, x in p] == [(130, "paused_no_buffer_growth", 30)], p
    assert r.stuck_frozen_s(159) == 0.0 and r.stuck_frozen_s(160) == 60.0 and r.stuck_frozen_s(199) == 99.0
    rec = r.record()
    assert rec["deadlocks"] == [{"t": 130, "paused_since": 100, "buffer_s": 0.4}] and rec["presses"] == 1
    r.step(S(paused=False), 200)                                           # resumed: cleared, the resume stamped
    assert r.stuck_frozen_s(260) == 0.0 and r.record()["deadlocks"][0]["resumed_at"] == 200
    # brad's startup rebuffer: readyState 2, buffered ahead ~0, under a stall label, 172 s, then resumes by itself
    r = B.SelfPauseResumer()
    seq = [(t, T(buf=0.0, ct=0.0, rs=2, stall=True)) for t in range(13, 185)] + [(185, S(paused=False))]
    assert [t for t, st in seq if r.step(st, t)] == [] and r.stuck_frozen_s(184) == 0.0
    for st in (T(rs=2), T(stall=True), T(ad=True), T(ended=True)):       # each part of the signature is needed
        r = B.SelfPauseResumer()
        assert [t for t in range(0, 120) if (x := r.step(st, t)) and x["reason"] == "paused_no_buffer_growth"] == [], st
    # growth resets the clock (buffer, or the playhead moving): 25 s flat, then a change, then flat -> press at change + 30
    for later in (T(buf=0.9), T(ct=106.0)):
        r = B.SelfPauseResumer()
        seq = [(t, T()) for t in range(0, 25)] + [(t, later) for t in range(25, 70)]
        assert [t for t, st in seq if (x := r.step(st, t)) and x["reason"] == "paused_no_buffer_growth"] == [55], later


def test_decision():
    assert (B.SELF_PAUSE_HOLD_S, B.SELF_PAUSE_MIN_BUFFER_S, B.RESUME_BACKOFF_S) == (2.0, 1.5, 10.0)
    # a self-pause with the buffer: eligible from 10, held 2 s, pressed at 12, its buffer and the pause duration logged
    p = presses([(t, S()) for t in range(10, 13)])
    assert p == [(12, {"action": "resume_after_self_pause", "reason": "self_pause", "buffer_s": 2.0, "paused_s": 2})], p
    assert presses([(t, S()) for t in range(10, 12)]) == []                      # 1 s: not yet
    # never in a stall, whatever the buffer (levi 00130: the after_stall press is gone), nor under the floor
    for st in (S(stall=True, buf=8.0), S(rs=2, stall=True)):
        assert presses([(t, st) for t in range(10, 120)]) == [], st      # levi 15:40: a pause in a stall never
    for st in (S(buf=1.4), S(buf=None)):                                  # under the floor: no self_pause press (the
        p = presses([(t, st) for t in range(10, 120)])                    # deadlock press, test_pause_deadlock, once)
        assert [x["reason"] for t, x in p] == ["paused_no_buffer_growth"], (st, p)
    # the floor reached and held: pressed 2 s after it
    seq = [(10, S(buf=0.5)), (11, S(buf=1.0)), (12, S(buf=1.6)), (13, S(buf=1.7)), (14, S(buf=1.8))]
    assert [t for t, _ in presses(seq)] == [14]
    # never: an ad, ended, no data, playing, no element
    for st in (S(ad=True), S(ended=True), S(rs=2), S(paused=False), None):
        assert presses([(t, st) for t in range(10, 30)]) == [], st
    # a stall label in the middle of the hold restarts it
    seq = [(10, S()), (11, S(stall=True)), (12, S()), (13, S()), (14, S())]
    assert [t for t, _ in presses(seq)] == [14]
    # a press that does not take is retried only after another hold; paused_s keeps counting
    p = presses([(t, S()) for t in range(10, 17)])
    assert [(t, x["paused_s"]) for t, x in p] == [(12, 2), (14, 4), (16, 6)], p
    # the back-off: a restart (paused -> playing, anyone's) holds the next press 10 s
    seq = [(10, S(paused=False)), (11, S())] + [(t, S()) for t in range(12, 30)]
    r = B.SelfPauseResumer()
    seq = [(5, S())] + [(10, S(paused=False))] + [(t, S()) for t in range(11, 30)]
    p = presses(seq, r)
    assert p[0][0] == 22 and all(t >= 20 for t, _ in p), p          # restart at 10: eligible from 20, pressed at 22
    rec = r.record()
    assert rec["presses"] == len(p) and rec["held_polls"]["backoff"] == 9 and rec["rule"] == B.RESUME_RULE, rec


def test_js():
    st = js(B._PLAYER_STATE_JS, stall=True, ad=True, paused=True, rs=3)
    assert json.loads(st["r"]) == {"paused": True, "rs": 3, "ended": False, "ad": True, "stall": True, "buf": 2.0, "ct": 100}, st
    assert json.loads(js(B._PLAYER_STATE_JS, buf_end=100.4)["r"])["buf"] == 0.4      # the playhead's own range
    assert js(B._PLAY_JS) == {"r": "pressed", "played": 1}                         # the largest visible video
    assert js(B._PLAY_JS, paused=False) == {"r": None, "played": 0}


def test_real_twitch_self_pause_fixture():
    """shagun's capture on chaos tw_12 (2026-10-17): the real player DOM has one <video> (the one the resume JS picks),
    and twitch paused it with readyState 4, no ad, the stall label off and its own button still reading "playing".
    That is the self-pause class: pressed once, after the 2 s hold (its buffer here set at the floor's side)."""
    html = (HERE / "fixtures" / "tw_player_dom_self_pause_2026-10-17.html").read_text()
    assert html.count("<video") == 1 and 'aria-label="Twitch video player"' in html
    fx = json.loads((HERE / "fixtures" / "tw_player_state_self_pause_2026-10-17.json").read_text())
    stall = str((fx.get("lvqoe") or {}).get("lvqoeStall", "0")) == "1"
    st = json.loads(js(B._PLAYER_STATE_JS, stall=stall, ad=bool(fx["ad"]), paused=bool(fx["paused"]), rs=int(fx["rs"]),
                       ended=bool(fx.get("ended")))["r"])
    assert st == {"paused": True, "rs": 4, "ended": False, "ad": False, "stall": False, "buf": 2.0, "ct": 100}, st
    r, made = B.SelfPauseResumer(), []
    for t in range(60):                               # the element plays once pressed
        if (p := r.step(st, 1000.0 + t)):
            made.append(p)
            st = dict(st, paused=False)
    assert made == [{"action": "resume_after_self_pause", "reason": "self_pause", "buffer_s": 2.0, "paused_s": 2.0}], made


def test_tw_100010_replay_no_press_in_the_loops():
    """levi 00130: on tw_100010's recorded element states (1 Hz ticks), the new rule presses nowhere in the stall loops;
    the old rule pressed 116 times, 114 after_stall, nearly all inside them."""
    fx = json.loads((HERE / "fixtures" / "tw_100010_replay.json").read_text())
    inside = lambda t: any(a <= t <= b for a, b in fx["loop_spans"])
    old = fx["presses_recorded"]
    # 116 recorded presses, 114 after_stall; 112 inside the spans as rounded in the fixture (0.1 s; 114 unrounded)
    assert len(old) == 116 and sum(1 for t, why in old if why == "after_stall") == 114
    assert sum(1 for t, _ in old if inside(t)) == 112
    r = B.SelfPauseResumer()
    made = []
    for t, paused, rs, buf, ad, stall in fx["ticks"]:
        p = r.step({"paused": bool(paused), "rs": rs, "buf": buf, "ad": bool(ad), "stall": bool(stall), "ended": False}, t)
        if p:
            made.append((t, p))
    assert not [t for t, _ in made if inside(t)], made
    assert all(p["buffer_s"] >= B.SELF_PAUSE_MIN_BUFFER_S for _, p in made), made
    assert r.record()["held_polls"].get("stall", 0) > 0                       # the old after_stall moments, held


class TwPage:
    """A twitch page for resume_press: `video_play_works` says whether video.play() sticks; the button's controller
    state, and how many button clicks make the element play (None: never)."""

    def __init__(self, video_play_works=False, button_state="paused", plays_after_clicks=1, button=True):
        self.paused, self.vp, self.state, self.need, self.button = True, video_play_works, button_state, plays_after_clicks, button
        self.clicks, self.calls = 0, []

    def eval(self, js):
        if js == B._PLAY_JS:
            self.calls.append("video_play")
            if self.vp:
                self.paused = False
            return "pressed"
        if js == B._PLAYER_STATE_JS:
            return json.dumps({"paused": self.paused, "rs": 4, "ended": False, "ad": False, "stall": False})
        if js == B._TW_BUTTON_JS % "true":
            if not self.button:
                return json.dumps({"present": False})
            out = {"present": True, "state": self.state, "label": "Play (space/k)" if self.state == "paused" else "Pause (space/k)"}
            self.clicks += 1
            self.calls.append("button")
            self.state = "playing" if self.state == "paused" else "paused"
            if self.need is not None and self.clicks >= self.need:
                self.paused = False
            return json.dumps(out)
        raise AssertionError(js[:40])


def test_twitch_resume_through_the_button():
    """t53 (peter's tw_100005 read): Twitch's controller undoes video.play(); a press re-paused within 1 s goes through
    Twitch's play button (a click when it says paused, a pause / play cycle when it says playing over the paused
    element); after TW_BUTTON_MAX_FAILS presses that leave it paused the collector stops pressing that pause (the frozen
    watchdog's re-entry is the fallback); every press and its outcome are in the record. yt / tt: video.play() only."""
    press = {"action": "resume_after_self_pause", "reason": "self_pause", "buffer_s": 2.0, "paused_s": 2.0}
    waits = []
    r = B.resume_press(TwPage(), "youtube", B.SelfPauseResumer(), press, waits.append)
    assert r == {**press, "via": "video_play"} and waits == [], r
    pg = TwPage(video_play_works=True)
    r = B.resume_press(pg, "twitch", B.SelfPauseResumer(), press, waits.append)
    assert r["outcome"] == "playing" and pg.calls == ["video_play"] and "button" not in r, r
    pg = TwPage(button_state="paused")
    r = B.resume_press(pg, "twitch", B.SelfPauseResumer(), press, waits.append)
    assert (r["outcome"], r["button"]["state"], r["button_outcome"], pg.clicks) == ("re_paused", "paused", "playing", 1), r
    pg = TwPage(button_state="playing", plays_after_clicks=2)          # the 2026-10-17 DOM: "Pause" over a paused element
    r = B.resume_press(pg, "twitch", B.SelfPauseResumer(), press, waits.append)
    assert r["button"]["state"] == "playing" and r["button_cycle"]["state"] == "paused" and r["button_outcome"] == "playing", r
    assert pg.clicks == 2 and B.TW_BUTTON_CYCLE_GAP_S in waits
    res, pg = B.SelfPauseResumer(hold_s=0.0), TwPage(plays_after_clicks=None)   # never plays: give up after the max
    st = {"paused": True, "rs": 4, "ended": False, "ad": False, "stall": False, "buf": 2.0}
    recs = []
    for k in range(10):
        p = res.step(st, 1000.0 + 2 * k)
        if p:
            recs.append(B.resume_press(pg, "twitch", res, p, waits.append))
    assert len(recs) == B.TW_BUTTON_MAX_FAILS and recs[-1]["gave_up"] and recs[0]["button_fails"] == 1, recs
    assert res.step(dict(st, paused=False), 1100.0) is None and not res.gave_up and res.button_fails == 0
    r = B.resume_press(TwPage(button=False), "twitch", B.SelfPauseResumer(), press, waits.append)
    assert r["button"] == {"present": False} and r["button_fails"] == 1, r


def test_button_js_on_the_fixture():
    """The button JS parses, and the real self-pause DOM holds the button it reads (data-a-target, the state attr)."""
    html = (HERE / "fixtures" / "tw_player_dom_self_pause_2026-10-17.html").read_text()
    assert 'data-a-target="player-play-pause-button"' in html and 'data-a-player-state="playing"' in html
    assert "!v.paused" in B._TW_BUTTON_JS and "skipped" in B._TW_BUTTON_JS     # never Twitch's pause on a playing element
    if shutil.which("node"):
        for arg in ("true", "false"):
            r = subprocess.run([shutil.which("node"), "--check", "-"], input=B._TW_BUTTON_JS % arg, capture_output=True, text=True)
            assert r.returncode == 0, r.stderr


def main() -> int:
    test_pause_deadlock()
    test_decision()
    test_tw_100010_replay_no_press_in_the_loops()
    test_twitch_resume_through_the_button()
    test_button_js_on_the_fixture()
    if not shutil.which("node"):
        print("SKIP (js): node not found")
        return 0
    test_js()
    test_real_twitch_self_pause_fixture()
    print("test_resume: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
