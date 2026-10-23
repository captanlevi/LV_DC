#!/usr/bin/env python3
"""v3 A14 (t67): the tier-change screenshot fires on A1's presented frame whose height changed, carrying the tick it
follows (A5) and that frame's presentedFrames; the res_meta trigger no longer fires while rVFC runs on the element, and
stays the trigger where rVFC is absent (no requestVideoFrameCallback, or the instruments-off arm). Drives the real
labeling/content.js under node with stubbed DOM / chrome APIs.

    python3 test_shot_trigger.py        (needs node; exit 0 = pass)
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
JS = HERE.parent / "labeling" / "content.js"
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const withRvfc = process.argv[3] === "1";
const timers = [], reqs = [];
let now = 1000, cb = null;
const v = { paused: false, ended: false, readyState: 4, networkState: 2, currentTime: 0, videoWidth: 1280, videoHeight: 720,
  clientWidth: 853, clientHeight: 480, playbackRate: 1, duration: Infinity, isConnected: true, currentSrc: "blob:x",
  srcObject: null, buffered: { length: 0 }, getVideoPlaybackQuality: () => ({ droppedVideoFrames: 0, totalVideoFrames: 0 }),
  addEventListener() {} };
if (withRvfc) v.requestVideoFrameCallback = (f) => { cb = f; };
const ds = {};
const dataset = new Proxy(ds, { set(t, k, val) { t[k] = val; if (k === "lvqoeShotReq") reqs.push(val); return true; } });
const ctx = { console: { log() {} }, performance: { timeOrigin: 5e11, now: () => now },
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {}, Date: { now: () => 7e11 + now },
  MutationObserver: class { observe() {} disconnect() {} }, XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
  location: { href: "https://www.youtube.com/@ch/live", hostname: "www.youtube.com" },
  chrome: { runtime: { lastError: null, sendMessage: (m, c) => c && c({ ok: true }) } },
  addEventListener() {}, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) };
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset }, body: {}, visibilityState: "visible",
  querySelectorAll: (q) => (q === "video" ? [v] : []), querySelector: () => null, getElementById: () => null,
  evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const tick = () => { now += 1000; for (const f of timers) f(); };
const frame = (w, h, pf) => { if (cb) { const f = cb; cb = null; f(now, { width: w, height: h, presentedFrames: pf, mediaTime: 0, presentationTime: now, expectedDisplayTime: now }); } };
tick(); frame(1280, 720, 10); tick();                     // first dims: no shot
v.videoHeight = 480; frame(854, 480, 40);                 // the switch, seen on the frame
tick();                                                   // the res_meta that also sees 480
frame(854, 480, 41); tick();
console.error(JSON.stringify({ reqs, tick: Number(ds.lvqoeTick) }));
"""


def _run(rvfc: bool) -> dict:
    node = shutil.which("node")
    assert node, "node is required"
    with tempfile.TemporaryDirectory() as tmp:
        h = Path(tmp) / "h.js"
        h.write_text(HARNESS)
        r = subprocess.run([node, str(h), str(JS), "1" if rvfc else "0"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stderr.strip().splitlines()[-1])


def test_frame_fires_with_rvfc():
    o = _run(True)
    tiers = [q for q in o["reqs"] if q.split("|")[1] == "tier_change"]
    assert len(tiers) == 1, o                                    # the frame's, and no second one from res_meta
    n, reason, tick_id, pf = tiers[0].split("|")
    assert reason == "tier_change" and pf == "pf=40" and tick_id != "null"


def test_res_meta_fires_without_rvfc():
    o = _run(False)
    tiers = [q for q in o["reqs"] if q.split("|")[1] == "tier_change"]
    assert len(tiers) == 1 and len(tiers[0].split("|")) == 3, o   # the old format, from the res_meta tick


def main() -> int:
    test_frame_fires_with_rvfc()
    test_res_meta_fires_without_rvfc()
    print("test_shot_trigger: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
