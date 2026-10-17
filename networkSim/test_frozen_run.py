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
play(5); frozen(3);
// the platform swaps in a replacement element that is still buffering, mid-stall: video_start clears the stall label,
// so the new element's own "stall" must be emitted (stall state is per element, re-evaluated on attach)
const w = mkVideo(); w.readyState = 2; videos = [w, v]; v.isConnected = false;
ctx.__attach(w); tick(); tick();
fs.writeFileSync(process.argv[3], JSON.stringify({ frozen_s: out, events: sent }));
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
    ok = abs(py - js_max) < 1e-6 and py > 0 and "stall" in after_swap
    print(json.dumps({"python_frozen_s": py, "content_js_max_frozen_s": js_max, "events": len(rows),
                      "stall_after_swap": "stall" in after_swap, "pass": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
