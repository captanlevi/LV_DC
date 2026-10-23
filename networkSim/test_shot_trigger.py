#!/usr/bin/env python3
"""v3 A14 (t67): the tier-change screenshot fires on A1's presented frame whose height changed, carrying the tick it
follows (A5) and that frame's presentedFrames; the res_meta trigger no longer fires while rVFC runs on the element, and
stays the trigger where rVFC is absent (no requestVideoFrameCallback, or the instruments-off arm) or silent for over
RVFC_FRESH_MS; an element swap compares its first frame with the last tick's height; side_counters.shot_triggers counts
tier-change shots by trigger (brad / levi 21:19). Drives the real
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
const withRvfc = process.argv[3] === "1", scen = process.argv[4] || "switch";
const timers = [], reqs = [], store = {};
let now = 1000;
const cbs = new Map();
const mk = (h) => { const v = { paused: false, ended: false, readyState: 4, networkState: 2, currentTime: 0, videoWidth: h * 16 / 9,
  videoHeight: h, clientWidth: 853, clientHeight: 480, playbackRate: 1, duration: Infinity, isConnected: true,
  currentSrc: "blob:x" + h, srcObject: null, buffered: { length: 0 },
  getVideoPlaybackQuality: () => ({ droppedVideoFrames: 0, totalVideoFrames: 0 }), addEventListener() {} };
  if (withRvfc) v.requestVideoFrameCallback = (f) => { cbs.set(v, f); };
  return v; };
const v = mk(720);
let videos = [v];
const ds = {};
const dataset = new Proxy(ds, { set(t, k, val) { t[k] = val; if (k === "lvqoeShotReq") reqs.push(val); return true; } });
const ctx = { console: { log() {} }, performance: { timeOrigin: 5e11, now: () => now },
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {}, Date: { now: () => 7e11 + now },
  MutationObserver: class { observe() {} disconnect() {} }, XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
  location: { href: "https://www.youtube.com/@ch/live", hostname: "www.youtube.com" },
  chrome: { runtime: { lastError: null, sendMessage: (m, c) => c && c({ ok: true }) },
            storage: { local: { get: (d, cb) => cb({ lvqoe_config: {} }), set: (o) => Object.assign(store, o) } } },
  addEventListener() {}, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) };
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset }, body: {}, visibilityState: "visible",
  querySelectorAll: (q) => (q === "video" ? videos : []), querySelector: () => null, getElementById: () => null,
  evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.__attach = (x, r) => attachVideo(x, r);", ctx);
const tick = () => { now += 1000; for (const f of timers) f(); };
const frame = (el, w, h, pf) => { const f = cbs.get(el); if (f) { cbs.delete(el); f(now, { width: w, height: h, presentedFrames: pf, mediaTime: 0, presentationTime: now, expectedDisplayTime: now }); } };
tick(); frame(v, 1280, 720, 10); tick();                  // first dims: no shot
if (scen === "switch") {
  v.videoHeight = 480; frame(v, 854, 480, 40);           // the switch, seen on the frame
  tick();                                                 // the res_meta that also sees 480
  frame(v, 854, 480, 41); tick();
} else if (scen === "swap_diff" || scen === "swap_same") {
  const h2 = scen === "swap_diff" ? 480 : 720;
  const u = mk(h2); videos = [u]; v.isConnected = false;
  ctx.__attach(u, "test");
  frame(u, h2 * 16 / 9, h2, 5);                           // the new element's first frame
  tick(); frame(u, h2 * 16 / 9, h2, 6); tick();
} else if (scen === "silent") {
  tick(); tick(); tick();                                 // rVFC registered but silent for 3 s
  v.videoHeight = 480; tick();                            // the tier tick must fire
}
console.error(JSON.stringify({ reqs, tick: Number(ds.lvqoeTick), counters: store.side_counters || null }));
"""


def _run(rvfc: bool, scen: str = "switch") -> dict:
    node = shutil.which("node")
    assert node, "node is required"
    with tempfile.TemporaryDirectory() as tmp:
        h = Path(tmp) / "h.js"
        h.write_text(HARNESS)
        r = subprocess.run([node, str(h), str(JS), "1" if rvfc else "0", scen], capture_output=True, text=True, timeout=60)
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


def _tiers(o):
    return [q for q in o["reqs"] if q.split("|")[1] == "tier_change"]


def test_swap_and_silent_rvfc():
    """brad / levi 21:19: a tier change carried by a new element gets one shot (vs the last tick's height), none at the
    same height; a registered but silent rVFC (3 s) hands the trigger back to the tick; the counter splits triggers."""
    o = _run(True, "swap_diff")
    assert len(_tiers(o)) == 1 and _tiers(o)[0].split("|")[3] == "pf=5", o
    assert _tiers(_run(True, "swap_same")) == []
    o = _run(True, "silent")
    assert len(_tiers(o)) == 1 and len(_tiers(o)[0].split("|")) == 3, o              # the tick's 3-field form
    assert o["counters"]["shot_triggers"]["tick"] == 1 and o["counters"]["shot_triggers"]["frame"] == 0
    o = _run(True, "switch")
    assert o["counters"]["shot_triggers"]["frame"] == 1 and o["counters"]["shot_triggers"]["tick"] == 0


def main() -> int:
    test_frame_fires_with_rvfc()
    test_res_meta_fires_without_rvfc()
    test_swap_and_silent_rvfc()
    print("test_shot_trigger: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
