#!/usr/bin/env python3
"""v3 A5 (t67): each page-stats row (yt stats for nerds / tw Video Stats, player_stats.jsonl) carries both clocks and
the content.js tick it follows: tick_id = the latest res_meta event_id, tick_ts its ts, wall = Date.now(); null before
the first tick. Drives the real labeling/content.js under node with stubbed DOM / chrome APIs.

    python3 test_page_stats_tick.py        (needs node; exit 0 = pass)
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
const timers = [], listeners = {}, stats = [], events = [];
let now = 1000;
const v = { paused: false, ended: false, readyState: 4, networkState: 2, currentTime: 0, videoWidth: 1280, videoHeight: 720,
  clientWidth: 853, clientHeight: 480, playbackRate: 1, duration: Infinity, isConnected: true, currentSrc: "blob:x",
  srcObject: null, buffered: { length: 0 }, getVideoPlaybackQuality: () => ({ droppedVideoFrames: 0, totalVideoFrames: 0 }),
  addEventListener() {} };
const ctx = { console: { log() {} }, performance: { timeOrigin: 5e11, now: () => now },
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {}, Date: { now: () => 7e11 + now },
  MutationObserver: class { observe() {} disconnect() {} }, XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
  location: { href: "https://www.youtube.com/@ch/live", hostname: "www.youtube.com" },
  chrome: { runtime: { lastError: null, sendMessage: (m, cb) => {
    if (m.kind === "player-stats-batch") stats.push(...m.payload);
    if (m.kind === "video-events-batch") events.push(...m.payload);
    cb && cb({ ok: true }); } } },
  addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) };
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: {} }, body: {}, visibilityState: "visible",
  querySelectorAll: (q) => (q === "video" ? [v] : []), querySelector: () => null, getElementById: () => null, evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const win = vm.runInContext("window", ctx);                 // the in-context window (e.source === window)
const post = (n) => { for (const f of listeners.message || []) f({ source: win, data: { source: "lvqoe-page-stats", ts: 5e11 + now, stats: { n } } }); };
post(0);                                                    // before any tick
const tick = () => { now += 1000; for (const f of timers) f(); };
tick(); post(1); tick(); post(2); tick();
const ticks = events.filter((e) => e.type === "res_meta").map((e) => [e.event_id, e.ts]);
console.error(JSON.stringify({ stats, ticks }));
"""


def test_rows_carry_their_tick():
    node = shutil.which("node")
    assert node, "node is required"
    with tempfile.TemporaryDirectory() as tmp:
        h = Path(tmp) / "h.js"
        h.write_text(HARNESS)
        r = subprocess.run([node, str(h), str(JS)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stderr.strip().splitlines()[-1])
    rows, ticks = {s["stats"]["n"]: s for s in o["stats"]}, o["ticks"]
    assert set(rows) == {0, 1, 2}, rows
    assert rows[0]["tick_id"] is None and rows[0]["tick_ts"] is None
    assert len(ticks) >= 2
    assert [rows[1]["tick_id"], rows[1]["tick_ts"]] == ticks[0] and [rows[2]["tick_id"], rows[2]["tick_ts"]] == ticks[1]
    assert all(isinstance(rows[n]["wall"], (int, float)) and rows[n]["wall"] > 7e11 for n in rows)


def main() -> int:
    test_rows_carry_their_tick()
    print("test_page_stats_tick: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
