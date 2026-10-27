#!/usr/bin/env python3
"""The ad set (levi 00176 / 14:35, supervisor 14:13; W-B yt_100003: an ad break stuck 1019 s over a PAUSED element with
9.6 s buffered, 1207 futile skip clicks, the ad_frozen clock held by the low-state hold):
  1. the ad_frozen hold lifts while the element is paused with data and flat for PAUSE_DEADLOCK_S, ad polls counted;
  2. yt's skip loop stops after SKIP_FUTILE_CLICKS clicks with the ad unchanged (skip_futile); the re-entry stays the
     ad_frozen clock's, tagged skip_futile;
  3. duration_finite alone never marks an ad (content.js isAdFrom; the skip loop does nothing on it);
  4. stuck-ad spans are recorded whatever the hold (meta.json ad_wait.ad_stuck_runs).
JS under node with stub DOMs.

    python3 test_ad_set.py        (exit 0 = pass)
"""
import json
import re
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
    _ws = types.ModuleType("websocket")             # the stub test_drain expects (sorted before it, never a bare module)
    _ws.WebSocketTimeoutException = type("WebSocketTimeoutException", (Exception,), {})
    _ws.WebSocket = object
    sys.modules["websocket"] = _ws
import browser as B  # noqa: E402

SKIP_STUB = r"""
const cfg = JSON.parse(process.argv[2]);
const ds = {};
const mp = {classList: {contains: c => cfg.showing && c === 'ad-showing'}, dataset: ds,
            querySelectorAll: () => []};
const v = {duration: cfg.duration, currentTime: 10, muted: false, dataset: {}, readyState: cfg.rs, paused: false,
           play() { return Promise.resolve(); }};
const root = {dataset: {}};
let clicks = 0, n = 0;
const btn = {offsetParent: {}, disabled: false, click() { clicks++; }};
const cdEl = {get textContent() { return cfg.cd_changes ? ('0:' + (30 - n)) : '0:21'; }};
global.document = {
  documentElement: root,
  getElementById: id => id === 'movie_player' ? mp : null,
  querySelector: s => {
    if (s === '#movie_player video' || s === 'video') return v;
    if (s.startsWith('.ytp-ad-duration-remaining')) return cfg.showing ? cdEl : null;
    if (cfg.button && s === '.ytp-ad-skip-button') return btn;
    return null;
  },
  querySelectorAll: () => [],
};
const out = [];
for (n = 0; n < cfg.calls; n++) out.push(eval(process.argv[1]));
console.log(JSON.stringify({out, clicks, ff: root.dataset.lvqoeFf ? JSON.parse(root.dataset.lvqoeFf) : null}));
"""


def skip_js(**cfg):
    cfg = {"showing": True, "duration": 3600, "button": True, "cd_changes": False, "calls": 15, "rs": 4, **cfg}
    r = subprocess.run([shutil.which("node"), "-e", SKIP_STUB, B._SKIP_AD_JS["youtube"], json.dumps(cfg)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def test_the_skip_loop_stops_after_ten_futile_clicks():
    """yt_100003: a visible skip button clicked about once a second for 17 min with the ad unchanged (duration 3600, so
    the fast-forward fallback never qualified). Now: SKIP_FUTILE_CLICKS clicks, then skip_futile, never another click."""
    r = skip_js()
    assert B.SKIP_FUTILE_CLICKS == 10 and r["clicks"] == 10
    assert all(x.startswith("skipped") for x in r["out"][:10]) and all(x.startswith("skip_futile") for x in r["out"][10:])


def test_a_moving_countdown_is_never_futile():
    r = skip_js(cd_changes=True)
    assert r["clicks"] == 15 and not any(x.startswith("skip_futile") for x in r["out"])


def test_duration_finite_alone_does_nothing_in_the_skip_loop():
    """levi 00176 (2c): a young live stream's DVR window under 600 s looks like a short ad duration."""
    r = skip_js(showing=False, duration=120, button=False, calls=3)
    assert r["out"] == [None, None, None] and r["clicks"] == 0


def test_content_js_needs_a_platform_signal():
    src = (HERE.parent / "labeling" / "content.js").read_text()
    fn = re.search(r"function isAdFrom\(sig\) \{.*?\n\}", src, re.S).group(0)
    r = subprocess.run([shutil.which("node"), "-e", fn + "; console.log(JSON.stringify([isAdFrom(['duration_finite']),"
                        " isAdFrom(['duration_finite','yt_ad_showing']), isAdFrom(['tw_ad_dom']), isAdFrom([])]))"],
                       capture_output=True, text=True, check=True)
    assert json.loads(r.stdout) == [False, True, True, False]
    assert "is_ad: isAdFrom(sig)" in src and "evt.is_ad = isAdFrom(sig);" in src and "sig.length > 0" not in src


PAUSED_AD = {"paused": True, "rs": 4, "buf": 9.6, "ct": 925.78, "ad": True, "ended": False, "stall": False}


def test_the_lift_counts_ad_polls():
    """The press trigger's run keeps excluding ads; the ad_frozen lift's run counts them."""
    r = B.SelfPauseResumer()
    for t in range(0, 40):
        r.step(dict(PAUSED_AD), 1000.0 + t)
    assert r.data_flat_since is None and r.data_flat_any_since == 1000.0
    r.step({**PAUSED_AD, "buf": 12.0}, 1041.0)                 # buffer growth starts a new run
    assert r.data_flat_any_since == 1041.0
    r.step({**PAUSED_AD, "paused": False}, 1042.0)
    assert r.data_flat_any_since is None


class _Sess:
    """BrowserSession's pieces _ad_frozen_held reads."""
    def __init__(self, low=True):
        self.resumer = B.SelfPauseResumer()
        self.frozen_hold_bypassed = []
        self._low = low

    def _lowstate_held(self, t):
        return self._low


def test_the_ad_frozen_hold_lifts_while_paused_with_data():
    s = _Sess()
    held = lambda t: B.BrowserSession._ad_frozen_held(s, t)      # noqa: E731
    for t in range(0, 29):
        s.resumer.step(dict(PAUSED_AD), 1000.0 + t)
        assert held(1000.0 + t)                                  # under PAUSE_DEADLOCK_S: still held
    for t in range(29, 40):
        s.resumer.step(dict(PAUSED_AD), 1000.0 + t)
    assert not held(1039.0) and not held(1039.5)
    assert len(s.frozen_hold_bypassed) == 1 and s.frozen_hold_bypassed[0]["clock"] == "ad_frozen"
    assert s.frozen_hold_bypassed[0]["lift"] == 1000.0 + B.PAUSE_DEADLOCK_S and s.frozen_hold_bypassed[0]["ad"] is True
    assert held(1039.0 + B.FROZEN_HOLD_POLL_MAX_AGE_S + 1)      # a stale poll: held again
    assert not B.BrowserSession._ad_frozen_held(_Sess(low=False), 1000.0)   # no low-state hold: never held


def test_yt_100003_re_enters_about_a_minute_after_the_lift_and_records_the_stuck_span():
    """The replay of yt_100003's shape: low states throughout, the element paused with data under the ad. Before, the
    clock reset every poll while held; now it counts from the lift and fires AD_FROZEN_MAX_S later, tagged skip_futile,
    and the span is recorded."""
    s = _Sess()
    w = B.PlayerErrorWatch()
    w.held_fn = lambda t: B.BrowserSession._ad_frozen_held(s, t)
    st = {"ad": True, "video": True, "rs": 4, "ct": 925.78, "paused": True, "cd": None}
    trig = None
    for t in range(0, 200):
        now = 1000.0 + t
        s.resumer.step(dict(PAUSED_AD), now)
        if t == 15:
            w.skip_futile_since = now
        trig = w.step(None, dict(st), now)
        if trig:
            break
    lift = 1000.0 + B.PAUSE_DEADLOCK_S
    assert trig and trig["reason"] == "ad_frozen" and trig["skip_futile"] is True
    assert lift + B.AD_FROZEN_MAX_S <= trig["t_trigger"] <= lift + B.AD_FROZEN_MAX_S + 2
    (run,) = w.ad_stuck_runs
    assert run["start"] == 1000.0 and run["end"] == trig["t_trigger"] and run["skip_futile"] and run["paused"] is True
    e = w.exposure()
    assert e["ad_stuck_runs"] == [run] and e["skip_futile_clicks"] == 10


def test_a_held_clock_still_records_the_stuck_span_and_a_moving_ad_records_none():
    w = B.PlayerErrorWatch()
    w.held_fn = lambda t: True                                   # held throughout (no lift): no trigger
    st = {"ad": True, "video": True, "rs": 4, "ct": 5.0, "paused": True, "cd": "0:21"}
    for t in range(0, 120):
        assert w.step(None, dict(st), 2000.0 + t) is None
    w.step(None, {"ad": False, "video": True, "rs": 4, "ct": 6.0, "paused": False}, 2120.0)
    assert len(w.ad_stuck_runs) == 1 and w.ad_stuck_runs[0]["s"] == 120.0
    w2 = B.PlayerErrorWatch()
    for t in range(0, 120):                                      # a real ad: the countdown moves
        w2.step(None, {**st, "cd": f"0:{59 - t // 2}"}, 3000.0 + t)
    assert w2.ad_stuck_runs == []


def test_no_fast_forward_below_readystate_2_and_its_state_logged():
    """t83 (11), W7 yt_100138 (levi 03:14): the second fast-forward came 0.8 s after the ad's loadedmetadata at
    readyState 1 and left the element paused at its end. Neither fast-forward (after two skip clicks; a finite ad) runs
    below readyState 2; each one stashes the element's state (dataset.lvqoeFf) for the ad log's ff field."""
    for rs in (0, 1):
        r = skip_js(duration=18.881, button=False, rs=rs, calls=3)
        assert not any(str(x).startswith("fast-forwarded") for x in r["out"]) and r["ff"] is None, r
        r = skip_js(duration=18.881, button=True, rs=rs, calls=5)
        assert not any(str(x).startswith("fast-forwarded") for x in r["out"]) and r["ff"] is None, r
    r = skip_js(duration=18.881, button=False, rs=2, calls=1)
    assert r["out"][0].startswith("fast-forwarded [") and r["ff"] == {"rs": 2, "ct": 10, "dur": 18.881, "paused": False}, r
    r = skip_js(duration=18.881, button=True, rs=4, calls=3)
    assert r["out"][2].startswith("fast-forwarded-after-skip") and r["ff"]["rs"] == 4, r


def test_an_ad_stuck_at_its_end_re_enters_30_s_into_skip_futile():
    """t83 (11), W7 yt_100138: the element paused at its own end (ct = dur 18.881, ended false, readyState 1), 393 s of
    skip_futile to the session's end, the ad_frozen clock held by the low state. ad_stuck_at_end fires AD_END_STUCK_S
    after skip_futile began over a paused element at its end, held or not (the hold recorded); never on a playing
    element, one short of its end, or without skip_futile."""
    assert (B.AD_END_STUCK_S, B.AD_END_TOL_S) == (30.0, 0.25)
    st = {"ad": True, "video": True, "rs": 1, "ct": 18.881, "paused": True, "cd": None, "dur": 18.881}
    for held in (True, False):
        w = B.PlayerErrorWatch()
        w.held_fn = lambda t, h=held: h
        trig = None
        for t in range(0, 60):
            if t == 5:
                w.skip_futile_since = 1000.0 + t
            trig = w.step(None, dict(st), 1000.0 + t)
            if trig:
                break
        assert trig and trig["reason"] == "ad_stuck_at_end" and trig["t_trigger"] == 1035.0, trig
        assert trig["since"] == 1005.0 and trig["held"] is held and trig["player"]["dur"] == 18.881, trig
    for other in ({"paused": False}, {"ct": 18.5}, {"dur": None}):
        w = B.PlayerErrorWatch()
        w.held_fn = lambda t: True
        w.skip_futile_since = 1000.0
        assert all(w.step(None, {**st, **other}, 1000.0 + t) is None for t in range(0, 120)), other
    w = B.PlayerErrorWatch()
    w.held_fn = lambda t: True
    assert all(w.step(None, dict(st), 1000.0 + t) is None for t in range(0, 120))      # no skip_futile: none


def test_wiring():
    src = (HERE / "browser.py").read_text()
    assert "self.error_watch.held_fn = self._ad_frozen_held" in src
    assert 'str(result).startswith("skip_futile")' in src
    assert 'str(result).startswith("fast-forwarded")' in src and "dataset.lvqoeFf" in src
    assert "dur: v && isFinite(v.duration) ? v.duration : null" in B._AD_PLAYER_JS


def main() -> int:
    test_the_skip_loop_stops_after_ten_futile_clicks()
    test_a_moving_countdown_is_never_futile()
    test_duration_finite_alone_does_nothing_in_the_skip_loop()
    test_content_js_needs_a_platform_signal()
    test_the_lift_counts_ad_polls()
    test_the_ad_frozen_hold_lifts_while_paused_with_data()
    test_yt_100003_re_enters_about_a_minute_after_the_lift_and_records_the_stuck_span()
    test_a_held_clock_still_records_the_stuck_span_and_a_moving_ad_records_none()
    test_no_fast_forward_below_readystate_2_and_its_state_logged()
    test_an_ad_stuck_at_its_end_re_enters_30_s_into_skip_futile()
    test_wiring()
    print("test_ad_set: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
