#!/usr/bin/env python3
"""v3 A1 (t67): content.js's requestVideoFrameCallback aggregates (side stream "frames"), under node with a stubbed
element whose requestVideoFrameCallback stores the callback and the harness presents frames:
  - aggregates only: no row per frame; a dims row at the first presented frame and at each dims change, with that
    frame's presentationTime / expectedDisplayTime / mediaTime / presentedFrames and the previous dims;
  - a first_frame row at the first frame after an attach and after a src_key change (tt's MediaStream anchor);
  - one tick row per 1 s res_meta tick, joined to its event_id, with presented / decoded / dropped side by side and the
    callbacks since the last tick, also while nothing is presented (cb 0, the last frame's values kept);
  - lvqoe_config.rvfc === false registers no callback and the tick rows say rvfc false (the gate's off arm).

    python3 test_rvfc_frames.py        (needs node; exit 0 = pass)
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const mode = process.argv[4];
const off = mode.startsWith("off");
const asyncCfg = mode.endsWith("async");
const pendingCfg = [];
const stored = {};
let failNextSide = mode === "on";        // the first side write fails once (re-queued, counted)
const timers = [], side = [], sent = [];
let now = 1000;
class MediaStream {}
const cbs = [];
let decoded = 0;
const v = { paused: false, ended: false, readyState: 4, networkState: 2, currentTime: 0, videoWidth: 1280,
  videoHeight: 720, clientWidth: 853, clientHeight: 480, playbackRate: 1, duration: Infinity, isConnected: true,
  currentSrc: "", srcObject: new MediaStream(), buffered: { length: 0 },
  getVideoPlaybackQuality: () => ({ droppedVideoFrames: 2, totalVideoFrames: decoded }),
  addEventListener() {}, requestVideoFrameCallback(f) { cbs.push(f); return cbs.length; } };
const ctx = {
  console: { log() {} }, performance: { timeOrigin: 5e11, now: () => now },
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {}, Date: { now: () => 5e11 + now },
  MutationObserver: class { observe() {} disconnect() {} },
  XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
  location: { href: "https://www.tiktok.com/@r/live", hostname: "www.tiktok.com" },
  chrome: { storage: { local: { get: (d, cb) => { const r = () => cb(off ? { lvqoe_config: { rvfc: false } } : d);
                                                    if (asyncCfg) pendingCfg.push(r); else r(); },
                                set: (o) => Object.assign(stored, o) } },
            runtime: { lastError: null, sendMessage: (m, cb) => {
              if (m.kind === "side-batch" && failNextSide) { failNextSide = false; cb && cb({ ok: false, err: "quota" }); return; }
              if (m.kind === "side-batch") side.push(m); if (m.kind === "video-events-batch") sent.push(...m.payload);
              cb && cb({ ok: true }); } } },
  addEventListener() {}, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
};
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: {} }, body: {}, visibilityState: "visible",
  querySelectorAll: (q) => (q === "video" ? [v] : []), querySelector: () => null, evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const registeredBeforeCfg = cbs.length;    // async config: nothing may register before the config is read
for (const r of pendingCfg.splice(0)) r();
let pf = 0, mt = 0;
const present = (n, w, h) => { for (let i = 0; i < n; i++) {
  const f = cbs.shift(); if (!f) return; now += 33; pf += 1; mt += 1 / 30; decoded += 1;
  f(now, { presentationTime: now - 1, expectedDisplayTime: now + 16, mediaTime: mt, presentedFrames: pf, width: w, height: h }); } };
const tick = () => { now += 1; for (const t of timers) t(); };
present(30, 720, 1280); tick();            // 30 frames, one tick
present(10, 720, 1280); present(5, 540, 960); tick();   // a rendition switch inside the second
tick();                                     // nothing presented in this second
v.srcObject = new MediaStream(); mt = 0;   // a new stream (tt re-entry): mediaTime restarts
present(3, 540, 960); tick();
vm.runInContext("flushSide()", ctx);        // the last tick's rows
fs.writeFileSync(process.argv[3], JSON.stringify({ side, sent, pending: cbs.length, registeredBeforeCfg, stored }));
"""


def run(mode: str) -> dict:
    node = shutil.which("node")
    assert node, "node not found: the content.js rVFC test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, out = Path(t) / "h.js", Path(t) / "out.json"
        h.write_text(HARNESS)
        subprocess.run([node, str(h), str(HERE.parent / "labeling" / "content.js"), str(out), mode], check=True)
        return json.loads(out.read_text())


def test_rvfc_frames():
    r = run("on")
    fr = [x for m in r["side"] if m["stream"] == "frames" for x in m["payload"]]
    kinds = [x["k"] for x in fr]
    assert len(fr) < 48 and kinds.count("tick") == 4, kinds                    # aggregates, never one row per frame
    dims = [x for x in fr if x["k"] == "dims"]
    assert [(d["w"], d["h"], d["prev_w"], d["prev_h"], d["pf"]) for d in dims] == \
        [(720, 1280, None, None, 1), (540, 960, 720, 1280, 41)], dims
    d = dims[1]
    assert d["ts"] == 5e11 + d["pt"] + 1 and d["edt"] == d["pt"] + 17 and abs(d["mt"] - 41 / 30) < 1e-9
    ff = [x for x in fr if x["k"] == "first_frame"]
    assert [(x["src"], x["prev_src"], x["pf"]) for x in ff] == [("MediaStream#1", None, 1), ("MediaStream#2", "MediaStream#1", 46)], ff
    assert abs(ff[1]["mt"] - 1 / 30) < 1e-9                                     # the new stream's own mediaTime
    ticks = [x for x in fr if x["k"] == "tick"]
    res_ids = [e["event_id"] for e in r["sent"] if e["type"] == "res_meta"]
    assert [t["tick"] for t in ticks] == res_ids
    assert [(t["cb"], t["pf"], t["decoded"], t["dropped"], t["rvfc"]) for t in ticks] == \
        [(30, 30, 30, 2, True), (15, 45, 45, 2, True), (0, 45, 45, 2, True), (3, 48, 48, 2, True)], ticks
    assert r["pending"] == 1                                                    # always one callback registered
    # brad 18:17: the first side write failed once: re-queued (no row lost, the 4 ticks above), counted, nothing dropped
    assert sum(ticks[-1]["side_failed"].values()) == 1 and ticks[-1]["side_dropped"] == {"player_events": 0, "frames": 0, "ad_edges": 0}
    assert all(t["off_arm_cb"] == 0 for t in ticks)
    sc = r["stored"]["side_counters"]                                          # levi 18:26: the final counters persisted
    assert (sc["side_dropped"], sc["side_failed"], sc["off_arm_cb"]) == (ticks[-1]["side_dropped"], ticks[-1]["side_failed"], 0)
    for mode in ("off", "off_async"):                                           # the off arm, config read late or early
        off = run(mode)
        fo = [x for m in off["side"] if m["stream"] == "frames" for x in m["payload"]]
        assert off["pending"] == 0 and off["registeredBeforeCfg"] == 0, mode
        assert {x["k"] for x in fo} == {"tick"} and all(x["rvfc"] is False and x["cb"] is None and x["off_arm_cb"] == 0 for x in fo)
    late = run("on_async")                                                      # on, config read after the attach
    fl = [x for m in late["side"] if m["stream"] == "frames" for x in m["payload"]]
    assert late["registeredBeforeCfg"] == 0 and late["pending"] == 1 and [x["k"] for x in fl][:2] == ["first_frame", "dims"]


def main() -> int:
    test_rvfc_frames()
    print("test_rvfc_frames: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
