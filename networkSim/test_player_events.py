#!/usr/bin/env python3
"""v3 A2 / A10 (t67): the raw player-event stream and element identity. Drives the real labeling/content.js and
labeling/background.js under node with stubbed DOM / chrome APIs:
  - every listed <video> event is recorded, undeduped: two resizes in uninterrupted play give two player_events
    records, while the label stream (emit's dedupe, unchanged) keeps one "resize";
  - each record carries both clocks, the element id, the src key and the element state; an attach carries its reason
    and the previous element;
  - el_id / src_key ride on every label event; a swapped element gets a new id; src keys for a blob URL, a MediaStream
    (tt) and a MediaSourceHandle (twitch); flushes go out as side-batch messages, never into the label events;
  - background.js stores each side batch under its own key; browser._side_streams joins them in key order.

    python3 test_player_events.py        (needs node; exit 0 = pass)
"""
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const timers = [];
const sent = [], side = [];
let now = 1000;
class MediaStream {}
class MediaSourceHandle {}
const mkVideo = (srcUrl, srcObject) => {
  const ls = {};
  return { paused: false, ended: false, readyState: 4, networkState: 2, currentTime: 0, videoWidth: 1280,
    videoHeight: 720, clientWidth: 853, clientHeight: 480, playbackRate: 1, duration: Infinity, isConnected: true,
    currentSrc: srcUrl || "", srcObject: srcObject || null, buffered: { length: 0 },
    getVideoPlaybackQuality: () => ({ droppedVideoFrames: 0, totalVideoFrames: 0 }),
    addEventListener(t, f) { (ls[t] = ls[t] || []).push(f); },
    fire(t) { for (const f of ls[t] || []) f(); } };
};
const v = mkVideo("blob:https://www.twitch.tv/1234-abcd");
let videos = [v];
const ctx = {
  console: { log() {} }, performance: { timeOrigin: 5e11, now: () => now },
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {}, Date: { now: () => 5e11 + now },
  MutationObserver: class { observe() {} disconnect() {} },
  XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
  location: { href: "https://www.twitch.tv/x", hostname: "www.twitch.tv" },
  chrome: { runtime: { lastError: null, sendMessage: (m, cb) => {
    if (m.kind === "video-events-batch") sent.push(...m.payload);
    if (m.kind === "side-batch") side.push(m);
    cb && cb({ ok: true }); } } },
  addEventListener() {}, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
};
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: {} }, body: {}, visibilityState: "visible",
  querySelectorAll: (q) => (q === "video" ? videos : []), querySelector: () => null,
  evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__attach = (v, r) => attachVideo(v, r); globalThis.__srcKey = srcKey;", ctx);
const tick = () => { now += 1000; for (const f of timers) f(); };
tick();
v.videoHeight = 480; v.fire("resize"); now += 10;           // two switches in uninterrupted play
v.videoHeight = 360; v.fire("resize"); now += 10;
v.fire("waiting"); v.fire("stalled"); v.fire("playing");
tick();
const u = mkVideo("", new MediaStream()); videos = [u, v]; v.isConnected = false;
tick();                                                      // detachment recovery re-attaches u
u.fire("resize");
tick();
const keys = { blob: ctx.__srcKey(v), ms: ctx.__srcKey(u), msh: ctx.__srcKey(mkVideo("", new MediaSourceHandle())),
               url: ctx.__srcKey(mkVideo("https://x.test/a.m3u8?token=1")), none: ctx.__srcKey(mkVideo("")) };
// background.js: two side batches stored under two keys
const store = {};
let bgListener = null;
const bg = { chrome: { runtime: { onMessage: { addListener: (f) => { bgListener = f; } } },
                       storage: { local: { set: (o, cb) => { Object.assign(store, o); cb && cb(); },
                                           get: (d, cb) => cb({ ...d, ...store }) } },
                       tabs: { onRemoved: { addListener() {} } } }, Date: { now: () => 42 }, console, String };
vm.createContext(bg);
vm.runInContext(fs.readFileSync(process.argv[4], "utf8"), bg);
const acks = [];
bgListener({ kind: "side-batch", stream: "player_events", payload: [{ ev: "resize", n: 1 }] }, { tab: { id: 7 } }, (r) => acks.push(r));
bgListener({ kind: "side-batch", stream: "player_events", payload: [{ ev: "resize", n: 2 }] }, { tab: { id: 7 } }, (r) => acks.push(r));
fs.writeFileSync(process.argv[3], JSON.stringify({ sent, side, keys, store, acks }));
"""


def run() -> dict:
    node = shutil.which("node")
    assert node, "node not found: the content.js player-event test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, out = Path(t) / "h.js", Path(t) / "out.json"
        h.write_text(HARNESS)
        lab = HERE.parent / "labeling"
        subprocess.run([node, str(h), str(lab / "content.js"), str(out), str(lab / "background.js")], check=True)
        return json.loads(out.read_text())


def test_player_events():
    r = run()
    pe = [x for m in r["side"] if m["stream"] == "player_events" for x in m["payload"]]
    evs = [x["ev"] for x in pe]
    # every element event, undeduped; the label stream keeps emit's dedupe (one "resize" for the two switches)
    assert evs.count("resize") == 3 and [x["h"] for x in pe if x["ev"] == "resize"][:2] == [480, 360], evs
    assert sum(1 for e in r["sent"] if e["type"] == "resize") == 2           # one per element (dedupe resets on attach)
    assert {"waiting", "stalled", "playing"} <= set(evs)
    att = [x for x in pe if x["ev"] == "lvqoe_attach"]
    assert [(a["reason"], a["el"], a["prev_el"]) for a in att] == [("initial", 1, None), ("detached", 2, 1)], att
    x = next(x for x in pe if x["ev"] == "resize")
    assert x["ts"] == 5e11 + x["pn"] and x["wall"] == 5e11 + x["pn"] and x["el"] == 1 and x["attached"] is True
    assert {"ct", "rs", "ns", "w", "h", "paused", "ended", "rate", "buf", "top", "src"} <= set(x)
    # el_id / src_key on the label events; a new element, a new id
    assert {(e["el_id"], e["src_key"]) for e in r["sent"]} == {(1, r["keys"]["blob"]), (2, "MediaStream#1")}
    k = r["keys"]
    assert k["blob"].startswith("blob:") and len(k["blob"]) == 13 and k["ms"] == "MediaStream#1"
    assert k["msh"] == "MediaSourceHandle#2" and k["url"].startswith("url:") and k["none"] == ""
    assert not any(m.get("kind") == "video-events-batch" for m in r["side"])
    # background: one key per batch, tabbed; the export joins them in key order
    keys = sorted(r["store"])
    assert len(keys) == 2 and all(x.startswith("side:player_events:") for x in keys) and r["acks"] == [{"ok": True}] * 2
    joined = B._side_streams({**r["store"], "events": [{"x": 1}], "player_stats": []})
    assert list(joined) == ["player_events"] and [x["n"] for x in joined["player_events"]] == [1, 2]
    assert all(x["tabId"] == 7 for x in joined["player_events"])


def main() -> int:
    test_player_events()
    print("test_player_events: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
