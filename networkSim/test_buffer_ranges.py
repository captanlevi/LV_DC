#!/usr/bin/env python3
"""v3 A6 (t67): buffer ranges. content.js's bufferedRanges records every video.buffered range at the tick as
"start:end;..." (media seconds, 3 dp) on each label event beside buffered_ahead_s; null when the element exposes no
ranges (tt's MediaStream source), never an empty string; browser.py's labels.csv header appends buffered_ranges.

    python3 test_buffer_ranges.py        (needs node; exit 0 = pass)
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
const ctx = { console: { log() {} }, performance: { timeOrigin: 5e11, now: () => 1000 }, setInterval() {},
  setTimeout: () => 0, clearInterval() {}, Date: { now: () => 5e11 }, MutationObserver: class { observe() {} disconnect() {} },
  XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 }, location: { href: "https://www.youtube.com/x", hostname: "www.youtube.com" },
  chrome: { runtime: { lastError: null, sendMessage: (m, cb) => cb && cb({ ok: true }) } }, addEventListener() {},
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) };
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: {} }, body: {}, visibilityState: "visible", querySelectorAll: () => [],
  querySelector: () => null, evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.__br = bufferedRanges; globalThis.__ba = bufferedAhead;", ctx);
const rng = (pairs) => ({ length: pairs.length, start: (i) => pairs[i][0], end: (i) => pairs[i][1] });
const v = (pairs, t) => ({ buffered: rng(pairs), currentTime: t });
const out = {
  two: ctx.__br(v([[10, 12.5], [13.0004, 20.12345]], 11)), ahead: ctx.__ba(v([[10, 12.5], [13, 20]], 11)),
  none: ctx.__br(v([], 0)), nobuf: ctx.__br({ currentTime: 1 }), nul: ctx.__br(null),
  src: fs.readFileSync(process.argv[2], "utf8").includes("buffered_ranges: bufferedRanges(video),"),
};
console.error(JSON.stringify(out));
"""


def test_ranges():
    node = shutil.which("node")
    assert node, "node is required"
    with tempfile.TemporaryDirectory() as tmp:
        h = Path(tmp) / "h.js"
        h.write_text(HARNESS)
        r = subprocess.run([node, str(h), str(JS)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stderr.strip().splitlines()[-1])
    assert o["two"] == "10.000:12.500;13.000:20.123" and o["ahead"] == 1.5
    assert o["none"] is None and o["nobuf"] is None and o["nul"] is None
    assert o["src"]


def test_header():
    src = (HERE / "browser.py").read_text()
    i = src.index('"el_id", "src_key",')
    assert '"buffered_ranges"]' in src[i:i + 200]


def main() -> int:
    test_ranges()
    test_header()
    print("test_buffer_ranges: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
