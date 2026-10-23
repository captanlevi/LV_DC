#!/usr/bin/env python3
"""v3 A3 / A7 / A9 / A11 (t67):
  - A3: ad_edges, event-driven: a MutationObserver on the attached player's container and the element's duration /
    metadata events re-evaluate the tick's own adSignals(); each change of the signal set is one record (both clocks),
    none while it is unchanged (driven under node with the real content.js);
  - A7: the frames tick row carries the player box: the visible part inside the viewport, the element size, the DPR and
    the visible fraction; and `top` (brad 18:28 (c));
  - A9: every net_labels row records apply_start (before `make slow` / `make rate`) and the apply kind;
  - A11: BrowserSession._clock_sample keeps time.time <-> page-clock samples with their round trip; an error is kept.

    python3 test_v3_a_rows.py        (needs node; exit 0 = pass)
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
const timers = [], side = [];
let now = 1000;
let obsCb = null, obsRoot = null, obsOpts = null;
let adLabel = false;
const ls = {};
const root = { parentElement: null, id: "root" };
let p = root;
for (let i = 0; i < 7; i++) p = { parentElement: p, id: "p" + i };
const v = { paused: false, ended: false, readyState: 4, networkState: 2, currentTime: 0, videoWidth: 1280, videoHeight: 720,
  clientWidth: 853, clientHeight: 480, playbackRate: 1, duration: Infinity, isConnected: true, currentSrc: "blob:x",
  srcObject: null, buffered: { length: 0 }, parentElement: p,
  getVideoPlaybackQuality: () => ({ droppedVideoFrames: 0, totalVideoFrames: 0 }),
  getBoundingClientRect: () => ({ left: -100, top: 50, right: 1180, bottom: 770, width: 1280, height: 720 }),
  addEventListener(t, f) { (ls[t] = ls[t] || []).push(f); }, fire(t) { for (const f of ls[t] || []) f(); } };
const label = { offsetParent: {}, getClientRects: () => [1] };
const ctx = {
  console: { log() {} }, performance: { timeOrigin: 5e11, now: () => now }, queueMicrotask: (f) => f(),
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {}, Date: { now: () => 5e11 + now },
  MutationObserver: class { constructor(cb) { this.cb = cb; } observe(r, o) { if (r !== ctx.document.documentElement) { obsCb = this.cb; obsRoot = r; obsOpts = o; } } disconnect() {} },
  XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 }, innerWidth: 1280, innerHeight: 720, devicePixelRatio: 2,
  location: { href: "https://www.twitch.tv/x", hostname: "www.twitch.tv" },
  chrome: { storage: { local: { get: (d, cb) => cb(d), set() {} } },
            runtime: { lastError: null, sendMessage: (m, cb) => { if (m.kind === "side-batch") side.push(m); cb && cb({ ok: true }); } } },
  addEventListener(t, f) { (winLs[t] = winLs[t] || []).push(f); },
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
};
const winLs = {};
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: {} }, body: {}, visibilityState: "visible", getElementById: () => null,
  querySelectorAll: (q) => (q === "video" ? [v] : (adLabel && q === '[data-a-target="video-ad-label"]' ? [label] : [])),
  querySelector: () => null, evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const tick = () => { now += 1000; for (const t of timers) t(); };
tick();
obsCb([]); obsCb([]);                     // DOM churn with no ad change: no edge
now += 5; adLabel = true; obsCb([]);      // the ad label appears: one edge
now += 5; obsCb([]);                      // unchanged
now += 5; v.duration = 15; v.fire("durationchange");      // the ad's finite duration: a second edge
now += 5; adLabel = false; v.duration = Infinity; v.fire("durationchange");   // back: a third edge
tick();
adLabel = true;                           // a marker change no observer saw (outside the subtree): the tick backstop
tick();
const inWin = vm.runInContext("window", ctx);   // the context's own window (a vm global proxy, not ctx)
for (const f of winLs.message || []) f({ source: inWin, data: { source: "lvqoe-ad-src", kind: "yt_player", ts: 5e11 + 7, pn: 7,
                                                              wall: 5e11 + 7, video_id: "AD123", player_state: 1 } });
for (const f of winLs.message || []) f({ source: {}, data: { source: "lvqoe-ad-src", kind: "yt_player", video_id: "X" } });
vm.runInContext("flushSide()", ctx);
fs.writeFileSync(process.argv[3], JSON.stringify({ side, rootId: obsRoot && obsRoot.id, opts: obsOpts }));
"""


def run() -> dict:
    node = shutil.which("node")
    assert node, "node not found: the content.js A3 / A7 test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, out = Path(t) / "h.js", Path(t) / "out.json"
        h.write_text(HARNESS)
        subprocess.run([node, str(h), str(HERE.parent / "labeling" / "content.js"), str(out)], check=True)
        return json.loads(out.read_text())


def test_ad_edges_and_box():
    r = run()
    assert r["rootId"] == "p1" and r["opts"]["subtree"] and "class" in r["opts"]["attributeFilter"]   # 6 ancestors up
    edges = [x for m in r["side"] if m["stream"] == "ad_edges" for x in m["payload"]]
    src3 = [e for e in edges if e["source"] == "yt_player"]                # levi 18:32 source 3, window messages only
    assert [(e["video_id"], e["player_state"], e["ts"]) for e in src3] == [("AD123", 1, 5e11 + 7)], src3
    edges = [e for e in edges if e["source"] == "dom"]
    assert [(e["signals"], e["prev"], e["reason"]) for e in edges] == [
        ("", None, "attach"), ("tw_ad_dom", "", "dom"), ("duration_finite|tw_ad_dom", "tw_ad_dom", "durationchange"),
        ("", "duration_finite|tw_ad_dom", "durationchange"), ("tw_ad_dom", "", "tick")], edges
    assert [e["is_ad"] for e in edges] == [False, True, True, False, True]
    pe = [x for m in r["side"] if m["stream"] == "player_events" for x in m["payload"]]
    root = [x for x in pe if x["ev"] == "lvqoe_ad_root"]
    assert [(x["root_id"], x["depth"], x["has_movie_player"]) for x in root] == [("p1", 6, False)], root
    tk = [x for m in r["side"] if m["stream"] == "frames" for x in m["payload"] if x["k"] == "tick"]
    assert sum(x["ad_cb"] for x in tk) == 4 and sum(x["ad_checks"] for x in tk) >= 6 and all(x["ad_ms"] >= 0 for x in tk)
    assert all(x["instruments"] is True for x in tk)
    e = edges[1]
    assert e["ts"] == 5e11 + e["pn"] and e["wall"] == 5e11 + e["pn"] and e["el"] == 1
    ticks = [x for m in r["side"] if m["stream"] == "frames" for x in m["payload"] if x["k"] == "tick"]
    box = ticks[-1]["box"]
    assert box == {"x": 0, "y": 50, "w": 1180, "h": 670, "el_w": 1280, "el_h": 720, "vw": 1280, "vh": 720, "dpr": 2,
                   "frac": round(1180 * 670 / (1280 * 720), 4)}, box
    assert all(t["top"] is True for t in ticks)


PS_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ivs = [], posted = [];
let now = 0, i = 0;
const ids = ["LIVE1", "LIVE1", "AD1", "AD1", "AD1", "LIVE1", "LIVE1"];
const player = { getVideoData: () => ({ video_id: ids[Math.min(i, ids.length - 1)] }), getPlayerState: () => 1 };
const ctx = { location: { hostname: "www.youtube.com" }, performance: { timeOrigin: 5e11, now: () => now },
  Date: { now: () => 5e11 + now }, setInterval: (f, ms) => ivs.push([f, ms]),
  document: { getElementById: (x) => (x === "movie_player" ? player : null), querySelectorAll: () => [], head: null },
  window: { postMessage: (m) => posted.push(m) } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const fast = ivs.filter(([f, ms]) => ms === 250).map(([f]) => f);
for (i = 0; i < ids.length; i++) { now += 250; for (const f of fast) f(); }
console.log(JSON.stringify(posted.filter(m => m.source === "lvqoe-ad-src")));
"""


def test_yt_player_source():
    """levi 18:32 source 3: page_stats.js posts the yt player's video id / state at 4 Hz, on change only."""
    node = shutil.which("node")
    assert node, "node not found (install node)"
    out = subprocess.run([node, "-e", PS_HARNESS, str(HERE.parent / "labeling" / "page_stats.js")], capture_output=True,
                         text=True, check=True)
    msgs = json.loads(out.stdout)
    assert [(m["video_id"], m["kind"], m["ts"] - 5e11) for m in msgs] == [("LIVE1", "yt_player", 250), ("AD1", "yt_player", 750),
                                                                          ("LIVE1", "yt_player", 1500)], msgs


def test_tw_playlist_source():
    """levi 18:32 source 2: MasterListener reads twitch's media playlists it already sees and records each flip of an
    ad marker; the master still gives the rung."""
    m = B.MasterListener("ws://x")
    sent = []

    def send(method, params, sid=None):
        sent.append((method, params, sid))
        return len(sent)
    media = "https://video-weaver.syd03.hls.ttvnw.net/v1/playlist/AbC.m3u8"
    m.handle({"method": "Network.responseReceived", "sessionId": "W", "params": {"requestId": "1", "response": {"url": media}}}, send)
    m.handle({"method": "Network.loadingFinished", "sessionId": "W", "params": {"requestId": "1"}}, send)
    assert sent[-1][0] == "Network.getResponseBody" and sent[-1][2] == "W"
    m.handle({"id": len(sent), "result": {"body": "#EXTM3U\n#EXTINF:2.0,\nseg1.ts\n"}}, send)
    m.playlist_ad('#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad"\n#EXTINF:2.0,\nad.ts', media, 10.0)
    m.playlist_ad('#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad"', media, 12.0)
    m.playlist_ad("#EXTINF:2.0,\nseg9.ts", media, 14.0)
    assert m.playlist_fetches == 4 and [(e["t"] if e["t"] != 0 else 0, e["in_ad"], e["prev"]) for e in m.ad_edges][1:] == \
        [(10.0, True, False), (14.0, False, True)] and m.ad_edges[0]["in_ad"] is False and m.ad_edges[0]["prev"] is None
    assert m.latest is None                                                     # a media playlist is never the rung
    src = (HERE / "exp.py").read_text()
    assert 'tw_playlist_ads=({"fetches": browser._master.playlist_fetches' in src


def test_apply_instants():
    from utils.dataModels import NetLabel
    n = NetLabel(timestamp=2.0, speed=900, state="7", apply_start=1.9, apply="slow")
    assert (n.apply_start, n.apply) == (1.9, "slow") and NetLabel(timestamp=1.0, speed=1, state="1").apply_start is None
    src = (HERE / "exp.py").read_text()
    i = src.index("t_apply = time.time()")
    assert i < src.index("_apply_net_stat(net_stat= net_stat)", i) < src.index("ts = time.time()", i)
    assert 'apply_start=t_apply, apply="rate" if net_stat.walk_step else "slow"' in src


def test_clock_sample():
    s = B.BrowserSession.__new__(B.BrowserSession)

    class Page:
        def eval(self, js, await_promise=False):
            assert "performance.timeOrigin + performance.now()" in js
            return 1.5e12
    s._page = Page()
    s._clock_sample("start")
    x = s.clock_offsets[0]
    assert x["reason"] == "start" and x["js_ms"] == 1.5e12 and x["rtt_ms"] >= 0
    assert abs(x["offset_ms"] - (x["js_ms"] - x["py_mid_ms"])) < 1e-3

    class Bad:
        def eval(self, js, await_promise=False):
            raise RuntimeError("gone")
    s._page = Bad()
    s._clock_sample("stop")
    assert s.clock_offsets[-1]["reason"] == "stop" and "gone" in s.clock_offsets[-1]["error"]
    src = (HERE / "browser.py").read_text()
    assert 'self._clock_sample("start")' in src and 'self._clock_sample("stop")' in src and "n % CLOCK_SAMPLE_S == 0" in src


def main() -> int:
    test_ad_edges_and_box()
    test_apply_instants()
    test_clock_sample()
    test_yt_player_source()
    test_tw_playlist_source()
    print("test_v3_a_rows: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
