#!/usr/bin/env python3
"""t83 (6), W1 tw_100113 +1615.5: content.js's element pick (pickBestVideo) never takes a rendered element whose box is
under PICK_SMALL_FRAC of the largest rendered one's (Twitch's 340x191 channel preview beside a self-paused main player:
the playing-first pick attached the preview, and its video_start cleared the page's stall label). Drives the real
labeling/content.js under node with a stub DOM.

    python3 test_pick_video.py        (needs node; exit 0 = pass)
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
const cases = JSON.parse(process.argv[3]);
const out = [];
for (const c of cases) {
  const mk = (e) => ({ paused: e.paused, ended: false, readyState: 4, networkState: 2, currentTime: 10,
    videoWidth: e.w, videoHeight: e.h, clientWidth: e.w, clientHeight: e.h, playbackRate: 1, duration: Infinity,
    isConnected: true, currentSrc: "blob:x" + e.name, srcObject: null, buffered: { length: 0 }, name: e.name,
    getVideoPlaybackQuality: () => ({ droppedVideoFrames: 0, totalVideoFrames: 0 }),
    getClientRects: () => (e.w ? [1] : []), addEventListener() {} });
  const videos = c.map(mk);
  const ctx = {
    console: { log() {} }, performance: { timeOrigin: 5e11, now: () => 1000 },
    setInterval: () => 0, setTimeout: () => 0, clearInterval() {}, Date: { now: () => 5e11 },
    MutationObserver: class { observe() {} disconnect() {} }, XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
    location: { href: "https://www.twitch.tv/x", hostname: "www.twitch.tv" },
    chrome: { runtime: { lastError: null, sendMessage: (m, cb) => cb && cb({ ok: true }) } },
    addEventListener() {}, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  };
  ctx.window = ctx; ctx.window.top = ctx;
  ctx.document = { documentElement: { dataset: {} }, body: {}, visibilityState: "visible",
    querySelectorAll: (q) => (q === "video" ? videos : []), querySelector: () => null,
    evaluate: () => ({ snapshotLength: 0 }) };
  vm.createContext(ctx);
  vm.runInContext(src + "\n;globalThis.__pick = () => pickBestVideo(); globalThis.__frac = PICK_SMALL_FRAC;" +
                  "globalThis.__side = () => sideBuffers.player_events;", ctx);
  const p = ctx.__pick();
  out.push({ pick: p ? p.name : null, frac: ctx.__frac,
             skipped: ctx.__side().filter((x) => x.ev === "lvqoe_pick_small_skipped").map((x) => [x.area, x.max_area]) });
}
fs.writeFileSync(process.argv[4], JSON.stringify(out));
"""


def run(cases: list) -> list:
    node = shutil.which("node")
    assert node, "node not found: the content.js pick test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, out = Path(t) / "h.js", Path(t) / "out.json"
        h.write_text(HARNESS)
        subprocess.run([node, str(h), str(HERE.parent / "labeling" / "content.js"), json.dumps(cases), str(out)],
                       check=True)
        return json.loads(out.read_text())


def V(name, w, h, paused):
    return {"name": name, "w": w, "h": h, "paused": paused}


def test_the_preview_is_never_picked_over_the_main_player():
    r = run([
        [V("main", 1348, 758, True), V("preview", 340, 191, False)],     # tw_100113: main self-paused, preview plays
        [V("main", 1348, 758, False), V("preview", 340, 191, False)],
        [V("preview", 340, 191, False)],                                    # alone: still picked
        [V("old", 1348, 758, True), V("new", 1348, 758, False)],           # a swap: the playing one, as before
        [V("hidden", 0, 0, False), V("main", 1348, 758, True)],            # unrendered: not excluded (as before)
        [V("a", 700, 400, True), V("b", 360, 203, False)],                 # over the fraction: playing first, as before
    ])
    assert r[0]["frac"] == 0.25
    assert [x["pick"] for x in r] == ["main", "main", "preview", "new", "hidden", "b"], r
    assert r[0]["skipped"] == [[340 * 191, 1348 * 758]] and all(x["skipped"] == [] for x in r[1:]), r


def main() -> int:
    if not shutil.which("node"):
        print("SKIP: node not found")
        return 0
    test_the_preview_is_never_picked_over_the_main_player()
    print("test_pick_video: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
