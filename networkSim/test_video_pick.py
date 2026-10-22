#!/usr/bin/env python3
"""content.js's sticky <video> pick (levi 03:22-03:23, peter's content-repick branch; parked: no landing without a shown
defect). Drives the real labeling/content.js under node with stubbed DOM / chrome APIs and a live MutationObserver
callback (every scripted tick also runs it, as twitch's chat does):

  - identity: with one <video>, on each platform (youtube / twitch / tiktok), every event's T* columns equal the golden
    written from T* d7433b03's content.js (fixtures/content_js_single_video_goldens_d7433b03.json; --write rebuilds it
    from a given T* content.js);
  - a second, small preview playing beside the main player: never attached;
  - twitch's self-pause (main paused) with a small preview playing: no swap;
  - a picture-by-picture ad (the main stream shrinks but plays; the ad element is larger and plays): no swap;
  - a momentary waiting (readyState < 2 for one poll) beside a larger playing element: no swap; a stuck one (no data,
    playhead unmoved over NOT_PLAYING_POLLS polls) beside a same-size playing element: swapped, reason
    replaced_same_size;
  - a dead element left in the DOM (paused) beside its same-size replacement that plays: swapped (replaced_same_size);
  - detached and ended: swapped with those reasons; every tick carries elem_id / elem_src_hash / n_videos.

    python3 test_video_pick.py                      (needs node; exit 0 = pass)
    python3 test_video_pick.py --write <T* content.js>
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "fixtures" / "content_js_single_video_goldens_d7433b03.json"
HOSTS = {"youtube": "www.youtube.com", "twitch": "www.twitch.tv", "tiktok": "www.tiktok.com"}
TSTAR_COLUMNS = ["ts", "type", "video_w", "video_h", "client_w", "client_h", "dropped", "current_time", "event_id",
                 "buffered_ahead_s", "playback_rate", "duration_s", "is_ad", "ad_signals", "src_changed",
                 "login_wall_text", "login_wall", "quality_countdown", "paused", "playhead_still_s", "ready_state",
                 "visibility", "total_frames"]

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [srcPath, host, scenario, outPath] = process.argv.slice(2);
const src = fs.readFileSync(srcPath, "utf8");
const timers = [], ds = {}, sent = [];
let moCb = null, now = 1e12, nextSrc = 0;
const mkVideo = (w, h, x = 0, y = 0) => {
  const v = { paused: false, ended: false, readyState: 4, currentTime: 0, videoWidth: 1280, videoHeight: 720,
    clientWidth: w, clientHeight: h, playbackRate: 1, duration: Infinity, isConnected: true,
    currentSrc: "blob:v" + (nextSrc++), buffered: { length: 0 },
    getVideoPlaybackQuality: () => ({ droppedVideoFrames: 0, totalVideoFrames: 0 }), addEventListener() {},
    _x: x, _y: y };
  v.getBoundingClientRect = () => ({ left: v._x, top: v._y, right: v._x + v.clientWidth, bottom: v._y + v.clientHeight,
                                     width: v.clientWidth, height: v.clientHeight });
  return v;
};
let videos = [mkVideo(1280, 720)];
const ctx = {
  console: { log() {} }, performance: { timeOrigin: 0, now: () => now },
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {},
  MutationObserver: class { constructor(cb) { moCb = cb; } observe() {} disconnect() {} },
  XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
  location: { href: "https://" + host + "/x", hostname: host },
  chrome: { runtime: { lastError: null, sendMessage: (m, cb) => { if (m.kind === "video-events-batch") sent.push(...m.payload); cb && cb({ ok: true }); } } },
  addEventListener() {}, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  innerWidth: 1920, innerHeight: 1080,
};
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: ds }, body: {}, visibilityState: "visible",
  querySelectorAll: (q) => (q === "video" ? videos : []), querySelector: () => null, getElementById: () => null,
  evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const mutate = () => { if (moCb) moCb([{ addedNodes: [{}] }]); };
const tick = () => { now += 1000; mutate(); for (const f of timers) f(); mutate(); };
const main = videos[0];
const play = (v, n) => { for (let i = 0; i < n; i++) { v.readyState = 4; v.currentTime += 1; tick(); } };
const marks = {};
const mark = (k) => { marks[k] = sent.length; };
if (scenario === "single") {
  play(main, 10);
  for (let i = 0; i < 5; i++) { main.readyState = 2; tick(); }         // a stall, playhead frozen
  play(main, 5);
  main.videoHeight = 480; main.videoWidth = 854; play(main, 5);        // a rendition change in the element
  main.duration = 15; play(main, 3); main.duration = Infinity;          // an ad-like duration
  main.paused = true; tick(); tick(); main.paused = false; play(main, 5);
} else if (scenario === "multi") {
  play(main, 5);
  mark("preview");                                                       // a small preview starts playing beside it
  const prev = mkVideo(320, 180, 1500, 800); videos = [prev, main]; play(main, 5);
  mark("selfpause");                                                     // twitch's self-pause; the preview keeps playing
  main.paused = true; for (let i = 0; i < 4; i++) { prev.currentTime += 1; tick(); } main.paused = false; play(main, 3);
  mark("pbyp");                                                          // picture-by-picture: main shrinks, plays; ad larger
  main.clientWidth = 640; main.clientHeight = 360; const ad = mkVideo(1280, 720, 640, 0); videos = [prev, ad, main];
  play(main, 4);
  mark("momentary");                                                     // one poll without data beside the larger ad
  main.readyState = 1; tick(); play(main, 3);
  mark("stuck");                                                         // no data and no progress, same-size element plays
  main.clientWidth = 1280; main.clientHeight = 720; videos = [prev, main];
  const repl = mkVideo(1280, 720); videos = [prev, main, repl];
  for (let i = 0; i < 4; i++) { main.readyState = 1; repl.currentTime += 1; tick(); }
  mark("dead_left");                                                     // the replacement is attached; now a dead one is left
  const repl2 = mkVideo(1280, 720); repl.paused = true; videos = [prev, repl, repl2];
  for (let i = 0; i < 3; i++) { repl2.currentTime += 1; tick(); }
  mark("detached");
  const repl3 = mkVideo(1280, 720); repl2.isConnected = false; videos = [prev, repl3];
  play(repl3, 3);
  mark("ended");
  const repl4 = mkVideo(1280, 720); repl3.ended = true; videos = [prev, repl3, repl4];
  play(repl4, 3);
  mark("end");
}
fs.writeFileSync(outPath, JSON.stringify({ events: sent, marks }));
"""


def run(content_js: Path, platform: str, scenario: str) -> dict:
    node = shutil.which("node")
    assert node, "node not found"
    with tempfile.TemporaryDirectory() as td:
        h, out = Path(td) / "h.js", Path(td) / "out.json"
        h.write_text(HARNESS)
        subprocess.run([node, str(h), str(content_js), HOSTS[platform], scenario, str(out)], check=True)
        return json.loads(out.read_text())


def single_rows(content_js: Path) -> dict:
    return {p: [{k: e.get(k) for k in TSTAR_COLUMNS} for e in run(content_js, p, "single")["events"]] for p in HOSTS}


def test_single_video_identity_on_every_platform():
    golden = json.loads(GOLDEN.read_text())
    got = single_rows(HERE.parent / "labeling" / "content.js")
    for p in HOSTS:
        assert len(got[p]) == len(golden[p]) and len(got[p]) > 30, (p, len(got[p]), len(golden[p]))
        for i, (a, b) in enumerate(zip(got[p], golden[p])):
            assert a == b, (p, i, {k: (a[k], b[k]) for k in a if a[k] != b[k]})


def _window(d: dict, a: str, b: str) -> list[dict]:
    return d["events"][d["marks"][a]:d["marks"][b]]


def test_sticky_pick():
    for p in HOSTS:
        d = run(HERE.parent / "labeling" / "content.js", p, "multi")
        ev = d["events"]
        first = ev[0]
        assert first["type"] == "video_start" and first["attach_reason"] == "initial", first
        main_id = first["elem_id"]
        assert all(e.get("elem_id") is not None and e.get("n_videos") >= 1 for e in ev if e["type"] == "res_meta")
        for a, b in (("preview", "selfpause"), ("selfpause", "pbyp"), ("pbyp", "momentary"), ("momentary", "stuck")):
            w = _window(d, a, b)
            assert {e["elem_id"] for e in w} == {main_id}, (p, a, sorted({e["elem_id"] for e in w}))
            assert not [e for e in w if e["type"] == "video_start"], (p, a)
        reasons = [e["attach_reason"] for e in ev if e["type"] == "video_start"]
        assert reasons == ["initial", "replaced_same_size", "replaced_same_size", "detached", "ended"], (p, reasons)
        stuck = _window(d, "stuck", "dead_left")
        assert stuck[-1]["elem_id"] != main_id, (p, "the stuck main was not replaced")
        assert len({e["elem_src_hash"] for e in ev if e["type"] == "res_meta"}) == 5, p


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--write":
        GOLDEN.write_text(json.dumps(single_rows(Path(sys.argv[2])), sort_keys=True) + "\n")
        print(f"wrote {GOLDEN.name} from {sys.argv[2]}")
        return 0
    test_single_video_identity_on_every_platform()
    test_sticky_pick()
    print("test_video_pick: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
