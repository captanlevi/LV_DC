#!/usr/bin/env python3
"""The collector presses play on a player that paused itself (levi 2026-10-17, chaos tw_10 / tw_12): no human ever pauses
these players, so every pause is a self-pause. browser.SelfPauseResumer decides (not an ad, paused, not ended,
readyState >= 3; after a stall at once, otherwise held for more than 3 s); _PLAYER_STATE_JS and _PLAY_JS run under node
with a stub DOM.

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
const [stall, ad, paused, rs, ended] = JSON.parse(process.argv[2]);
let played = 0;
const small = {paused: false, readyState: 4, ended: false, clientWidth: 160, clientHeight: 90, getClientRects: () => [1],
               play() { throw new Error('pressed the small video'); }};
const v = {paused, readyState: rs, ended, clientWidth: 800, clientHeight: 450, getClientRects: () => [1],
           play() { played++; return Promise.resolve(); }};
global.document = {documentElement: {dataset: {lvqoeStall: stall ? '1' : '0', lvqoeAd: ad ? '1' : '0'}},
                   querySelectorAll: () => [small, v]};
const r = eval(process.argv[1]);
console.log(JSON.stringify({r, played}));
"""


def js(src, stall=False, ad=False, paused=True, rs=4, ended=False):
    out = subprocess.run([shutil.which("node"), "-e", STUB, src, json.dumps([stall, ad, paused, rs, ended])],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def presses(states, hold_s=B.SELF_PAUSE_HOLD_S):
    """Run the resumer over (t, state) at the watcher's 1 Hz; the presses it makes."""
    r = B.SelfPauseResumer(hold_s)
    return [(t, p) for t, st in states if (p := r.step(st, t))]


def S(paused=True, rs=4, stall=False, ad=False, ended=False):
    return {"paused": paused, "rs": rs, "stall": stall, "ad": ad, "ended": ended}


def test_decision():
    # no_stall: held > 3 s, so the first press is at the 4th second of the hold, and the pause duration is recorded
    p = presses([(t, S()) for t in range(10, 15)])
    assert p == [(14, {"action": "resume_after_self_pause", "reason": "no_stall", "paused_s": 4})], p
    assert presses([(t, S()) for t in range(10, 14)]) == []                      # exactly 3 s: not yet
    # after_stall: the player paused in a stall (readyState < 3), refilled -> pressed at once
    p = presses([(10, S(rs=2, stall=True)), (11, S(rs=2, stall=True)), (12, S(stall=True))])
    assert p == [(12, {"action": "resume_after_self_pause", "reason": "after_stall", "paused_s": 2})], p
    # a stall label seen earlier in the same pause keeps the reason after_stall
    p = presses([(10, S(rs=2, stall=True))] + [(t, S()) for t in range(11, 16)])
    assert [x["reason"] for _, x in p] == ["after_stall"], p
    # never: an ad, ended, no data, playing, no element
    for st in (S(ad=True), S(ended=True), S(rs=2), S(paused=False), None):
        assert presses([(t, st) for t in range(10, 30)]) == [], st
    # an ad in the middle of the hold restarts it
    seq = [(10, S()), (11, S()), (12, S(ad=True)), (13, S()), (14, S()), (15, S()), (16, S()), (17, S())]
    assert [t for t, _ in presses(seq)] == [17]
    # a press that does not take is retried only after another hold; paused_s keeps counting
    p = presses([(t, S()) for t in range(10, 23)])
    assert [(t, x["paused_s"]) for t, x in p] == [(14, 4), (18, 8), (22, 12)], p
    # playback resuming ends the pause: a new pause starts its own hold and duration
    seq = [(t, S()) for t in range(10, 15)] + [(15, S(paused=False))] + [(t, S()) for t in range(16, 21)]
    assert [(t, x["paused_s"]) for t, x in presses(seq)] == [(14, 4), (20, 4)]


def test_js():
    st = js(B._PLAYER_STATE_JS, stall=True, ad=True, paused=True, rs=3)
    assert json.loads(st["r"]) == {"paused": True, "rs": 3, "ended": False, "ad": True, "stall": True}, st
    assert js(B._PLAY_JS) == {"r": "pressed", "played": 1}                         # the largest visible video
    assert js(B._PLAY_JS, paused=False) == {"r": None, "played": 0}


def test_real_twitch_self_pause_fixture():
    """shagun's capture on chaos tw_12 (2026-10-17): the real player DOM has one <video> (the one the resume JS picks),
    and twitch paused it with readyState 4, no ad, the stall label off and its own button still reading "playing".
    That is the no_stall class: under the old stall-only rule it was resumed 0 times; now once, after the 3 s hold."""
    html = (HERE / "fixtures" / "tw_player_dom_self_pause_2026-10-17.html").read_text()
    assert html.count("<video") == 1 and 'aria-label="Twitch video player"' in html
    fx = json.loads((HERE / "fixtures" / "tw_player_state_self_pause_2026-10-17.json").read_text())
    stall = str((fx.get("lvqoe") or {}).get("lvqoeStall", "0")) == "1"
    st = json.loads(js(B._PLAYER_STATE_JS, stall=stall, ad=bool(fx["ad"]), paused=bool(fx["paused"]), rs=int(fx["rs"]),
                       ended=bool(fx.get("ended")))["r"])
    assert st == {"paused": True, "rs": 4, "ended": False, "ad": False, "stall": False}, st
    r, made = B.SelfPauseResumer(), []
    for t in range(60):                               # the element plays once pressed
        if (p := r.step(st, 1000.0 + t)):
            made.append(p)
            st = dict(st, paused=False)
    assert made == [{"action": "resume_after_self_pause", "reason": "no_stall", "paused_s": 4.0}], made


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
    press = {"action": "resume_after_self_pause", "reason": "after_stall", "paused_s": 1.0}
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
    st = {"paused": True, "rs": 4, "ended": False, "ad": False, "stall": True}
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
    if shutil.which("node"):
        for arg in ("true", "false"):
            r = subprocess.run([shutil.which("node"), "--check", "-"], input=B._TW_BUTTON_JS % arg, capture_output=True, text=True)
            assert r.returncode == 0, r.stderr


def main() -> int:
    test_decision()
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
