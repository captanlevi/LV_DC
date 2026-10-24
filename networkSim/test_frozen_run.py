#!/usr/bin/env python3
"""content.js's frozen-playhead run (<html data-lvqoe-frozen-s>, read by browser.py's watchdog) equals
check_session.frozen_playhead_run() on the stream content.js emits (addendum N). Drives the real labeling/content.js
under node with stubbed DOM / chrome APIs: a play -> stall with a frozen playhead -> ad -> frozen again -> recovery
script, ticked through the stall safety net and the res_meta poll. Also checks that a new <video> re-evaluates the stall
state (a buffering replacement element gets its own "stall").

    python3 test_frozen_run.py        (needs node; exit 0 = pass)
"""
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session  # noqa: E402

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const timers = [];
const ds = {};
const sent = [];
const mkVideo = () => ({ paused: false, ended: false, readyState: 4, currentTime: 0, videoWidth: 1280, videoHeight: 720,
  clientWidth: 853, clientHeight: 480, playbackRate: 1, duration: Infinity, isConnected: true, currentSrc: "blob:a",
  buffered: { length: 0 }, getVideoPlaybackQuality: () => ({ droppedVideoFrames: 0 }), addEventListener() {} });
let videos = [mkVideo()];
let now = 1e12;
const ctx = {
  console: { log() {} }, performance: { timeOrigin: 0, now: () => now },
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {},
  MutationObserver: class { observe() {} disconnect() {} },
  XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
  location: { href: "https://www.twitch.tv/x", hostname: "www.twitch.tv" },
  chrome: { runtime: { lastError: null, sendMessage: (m, cb) => { if (m.kind === "video-events-batch") sent.push(...m.payload); cb && cb({ ok: true }); } } },
  addEventListener() {}, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
};
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: ds }, body: {}, visibilityState: "visible",
  querySelectorAll: (q) => (q === "video" ? videos : []), querySelector: () => null,
  evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__attach = (v) => attachVideo(v);", ctx);
const out = [];
const tick = () => { now += 1000; for (const f of timers) f(); out.push(Number(ds.lvqoeFrozenS || 0)); };
const v = videos[0];
const play = (n) => { for (let i = 0; i < n; i++) { v.readyState = 4; v.currentTime += 1; tick(); } };
const frozen = (n) => { for (let i = 0; i < n; i++) { v.readyState = 2; tick(); } };
play(10); frozen(40);
v.duration = 15; frozen(5); v.duration = Infinity;   // an ad: is_ad ticks end the run
frozen(30);
v.currentTime += 0.5; frozen(10);                    // a sub-tolerance nudge keeps the run
// levi (tw_10): a player paused during a stall that has data again (readyState 4) gets no "start" until it plays
const startsBefore = sent.filter(e => e.type === "start").length;
v.paused = true; frozen(2); v.readyState = 4; tick(); tick();
const startWhilePaused = sent.filter(e => e.type === "start").length - startsBefore;
v.paused = false; tick();
const startAfterPlay = sent.filter(e => e.type === "start").length - startsBefore;
play(12); frozen(3);                                 // a real advance (>= 10 advancing ticks) ends a run
// chaos tw_27 (2026-10-18): a frozen player under the spinner; the resumer presses play (pause / start events clear
// the stall label) and twitch re-attaches a new <video> at the same playhead (video_start). Under addendum N's measure
// (restored 2026-10-19, levi) a tick without the stall label ends the run, so the press ends it; the 2026-10-18 measure
// (reverted) carried the run through both. Parity is checked on this window's own events.
play(12);
const tw27From = out.length;
const tw27Ev = sent.length;
for (let i = 0; i < 20; i++) { v.readyState = 2; tick(); }
v.paused = true; tick(); v.paused = false; v.readyState = 4; tick(); tick();       // the resume press: label to playing
for (let i = 0; i < 20; i++) { v.readyState = 2; tick(); }
const u = mkVideo(); u.currentTime = v.currentTime; u.readyState = 2; videos = [u, v];
ctx.__attach(u);
for (let i = 0; i < 20; i++) { tick(); }                                           // the re-attached element, still frozen
// tw_27's second element: 4-tick play bursts from 0 between re-attaches to the stuck one are not a real advance
const b = mkVideo(); b.currentTime = 0;
for (let k = 0; k < 2; k++) {
  videos = [b, u]; ctx.__attach(b); for (let i = 0; i < 4; i++) { b.currentTime += 1; tick(); }
  videos = [u, b]; ctx.__attach(u); for (let i = 0; i < 5; i++) { tick(); }
}
videos = [v]; ctx.__attach(v); play(12);                                            // a real advance ends the run
const tw27Max = Math.max(...out.slice(tw27From));
const tw27EvEnd = sent.length;
// the platform swaps in a replacement element that is still buffering, mid-stall: video_start clears the stall label,
// so the new element's own "stall" must be emitted (stall state is per element, re-evaluated on attach)
const w = mkVideo(); w.readyState = 2; videos = [w, v]; v.isConnected = false;
ctx.__attach(w); tick(); tick();
// WA2 r1 ti_100001 (levi 00:28 / 00:30): the clock runs, readyState 4, not paused, no stall label, and no frame is decoded
// (totalVideoFrames flat): the frozen PICTURE run; a counter reset decoding 1 frame is bridged; frames again end it
const pv = mkVideo(); let ptf = 100; pv.getVideoPlaybackQuality = () => ({ droppedVideoFrames: 0, totalVideoFrames: ptf });
videos = [pv]; ctx.__attach(pv);
for (let i = 0; i < 4; i++) { pv.currentTime += 1; ptf += 30; tick(); }
const picEv = sent.length;                           // the window opens at the last playing tick (the run's first tick)
pv.currentTime += 1; ptf += 30; tick();
const picFrom = out.length;
for (let i = 0; i < 100; i++) { pv.currentTime += 1; tick(); }
ptf = 5; pv.currentTime += 1; tick();                // a reset with the new counter >= 2 BRIDGES (brad 00:45 M2)
for (let i = 0; i < 20; i++) { pv.currentTime += 1; tick(); }
for (let i = 0; i < 5; i++) { pv.currentTime += 1; ptf += 30; tick(); }
const picMax = Math.max(...out.slice(picFrom)), picEvEnd = sent.length, picLast = out[out.length - 1];
// brad 00:45 M1: the stall label on with the clock creeping (< the 1 s tolerance per stall run) and no frame decoded: a
// stall-only run and a picture run over the same seconds, then the picture alone: ONE union run on both sides
for (let i = 0; i < 5; i++) { pv.currentTime += 1; ptf += 30; tick(); }
const ovEv = sent.length, ovFrom = out.length;
pv.currentTime += 1; ptf += 30; tick();
for (let i = 0; i < 30; i++) { pv.readyState = 2; pv.currentTime += 0.2; tick(); }
pv.readyState = 4;
for (let i = 0; i < 30; i++) { pv.currentTime += 1; tick(); }
for (let i = 0; i < 5; i++) { pv.currentTime += 1; ptf += 30; tick(); }
const ovMax = Math.max(...out.slice(ovFrom)), ovEvEnd = sent.length;
fs.writeFileSync(process.argv[3], JSON.stringify({ frozen_s: out, events: sent, startWhilePaused, startAfterPlay, tw27Max,
                                                   tw27Ev, tw27EvEnd, picMax, picEv, picEvEnd, picLast, ovMax, ovEv,
                                                   ovEvEnd }));
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node not found")
        return 0
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "h.js"
        h.write_text(HARNESS)
        res = Path(td) / "out.json"
        subprocess.run([node, str(h), str(HERE.parent / "labeling" / "content.js"), str(res)], check=True)
        d = json.loads(res.read_text())
    rows = [{k: ("" if v is None else str(v)) for k, v in e.items()} for e in d["events"]]
    py = check_session.frozen_playhead_run(rows)
    js_max = max(d["frozen_s"])
    types = [e["type"] for e in d["events"]]
    after_swap = types[types.index("video_start", 1):] if types.count("video_start") > 1 else []
    runs = check_session.frozen_runs_all(rows[d["tw27Ev"]:d["tw27EvEnd"]])         # the tw_27-like window's events only
    tw27_py = max((b - a for a, b in runs), default=0.0)
    pic = check_session.frozen_runs_all(rows[d["picEv"]:d["picEvEnd"]])           # the frozen-picture window's events
    pic_py = max((b - a for a, b in pic), default=0.0)
    ov = check_session.frozen_runs_all(rows[d["ovEv"]:d["ovEvEnd"]])             # stall + picture over the same seconds
    ov_py = max((b - a for a, b in ov), default=0.0)
    # addendum N: the resume press ends the run (< 80 s; the reverted 2026-10-18 measure read >= 80 s), JS = Python;
    # the frozen picture (121 s of clock with <= 1 frame per tick, a reset bridged) is one run on both sides, ended by frames
    ok = (abs(py - js_max) < 1e-6 and py > 0 and "stall" in after_swap and d["startWhilePaused"] == 0
          and d["startAfterPlay"] >= 1 and 0 < d["tw27Max"] < 80 and abs(tw27_py - d["tw27Max"]) < 1e-6
          and d["picMax"] >= 120 and abs(pic_py - d["picMax"]) < 1e-6 and d["picLast"] == 0
          and d["ovMax"] >= 59 and abs(ov_py - d["ovMax"]) < 1e-6 and len([r for r in ov if r[1] - r[0] >= 59]) == 1)
    print(json.dumps({"python_frozen_s": py, "content_js_max_frozen_s": js_max, "events": len(rows),
                      "stall_after_swap": "stall" in after_swap, "start_while_paused": d["startWhilePaused"],
                      "start_after_play": d["startAfterPlay"], "tw27_like_run_js_py": [d["tw27Max"], tw27_py],
                      "frozen_picture_js_py": [d["picMax"], pic_py], "overlap_union_js_py": [d["ovMax"], ov_py],
                      "pass": ok}))
    return 0 if ok else 1


def _row(t, ty="res_meta", ct=None, tf=None, paused=False):
    return {"ts": str(t * 1000), "type": ty, "current_time": "" if ct is None else str(ct),
            "total_frames": "" if tf is None else str(tf), "paused": str(paused), "is_ad": "False"}


def test_union_and_reset_bridge_in_python():
    """brad 00:45: a re-entry inside an overlapping stall + picture run is excluded exactly once (M1); a reset with the
    new counter >= 2 bridges the picture run (M2)."""
    rows = [_row(0, "res_meta", 0.0, 10), _row(0.5, "stall")]
    ct = 0.0
    for t in range(1, 61):                       # stall label on, the clock creeping, no frame: both runs
        ct += 0.01
        rows.append(_row(t, ct=ct, tf=10))
    rows.append(_row(60.5, "start"))
    for t in range(61, 101):                     # the picture alone
        ct += 1.0
        rows.append(_row(t, ct=ct, tf=10))
    rows.append(_row(101, ct=ct + 1, tf=60))     # frames: the run ends
    runs = check_session.frozen_runs_all(rows)
    assert len(runs) == 1 and runs[0][0] == 0.0 and runs[0][1] == 100.0, runs
    r = check_session.recovered_frozen(rows, [{"outcome": "continue", "t_nav": 50.0}])
    assert r["recovered"] is not None and r["max_s"] == 0.0           # excluded once, nothing left over
    rows2 = [_row(0, ct=0.0, tf=100)] + [_row(t, ct=float(t), tf=100) for t in range(1, 40)]
    rows2 += [_row(40, ct=0.5, tf=7)] + [_row(t, ct=0.5 + t - 40, tf=7) for t in range(41, 80)]
    rows2 += [_row(80, ct=41.0, tf=60)]
    pr = check_session.frozen_picture_runs(rows2)
    assert pr == [(0.0, 79.0)], pr                                       # one run across the reset (new counter 7)


def test_content_js_frozen_run_equals_check_session():
    """The parity as a pytest that fails (levi t58, F-5 / B-1): without node it fails, it never skips."""
    assert shutil.which("node"), "node not found: the content.js = check_session parity cannot run (install node)"
    assert main() == 0


if __name__ == "__main__":
    test_union_and_reset_bridge_in_python()
    sys.exit(main())
