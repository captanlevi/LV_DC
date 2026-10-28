#!/usr/bin/env python3
"""t93 rows 9 / 20 (levi 00124: "the wrap passes arguments, this and return through unchanged, synchronously, plus one
push; a test asserts it"): browser.MSE_WRAP_JS under node with stub MediaSource / SourceBuffer prototypes:
  - every wrapped call reaches the original synchronously with the same this and the same argument objects, and returns
    the original's value; the original's exception reaches the caller unchanged;
  - a recording fault (a buffer whose byteLength throws) never reaches the page: the original still runs and returns;
  - records: add (mime, id), app (bytes), rm (start, end), ct (mime), end (updateend: buffered end, range count), batched
    once per 1 s as one postMessage {source: "lvqoe-mse", recs, over}; past the cap counted (over);
  - OFF blocks (epoch clock, even block) record nothing while the wrap stays installed; a second injection is a no-op.

    python3 test_mse_wrap.py        (needs node; exit 0 = pass)
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
const [src, outPath, nowMs] = [fs.readFileSync(process.argv[2], "utf8"), process.argv[3], Number(process.argv[4])];
const calls = [], posted = [], timers = [];
const SENT = { ret: "append-return" };
class SourceBuffer {
  constructor() { this.ls = {}; this.buffered = { length: 1, end: () => 12.5 }; }
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); }
  fire(t) { for (const f of this.ls[t] || []) f(); }
}
SourceBuffer.prototype.appendBuffer = function (buf) { calls.push({ m: "appendBuffer", self: this, args: [...arguments] }); return SENT; };
SourceBuffer.prototype.remove = function (s, e) { calls.push({ m: "remove", self: this, args: [...arguments] });
  if (s < 0) throw new RangeError("bad range"); };
SourceBuffer.prototype.changeType = function (t) { calls.push({ m: "changeType", self: this, args: [...arguments] }); };
class MediaSource {}
MediaSource.prototype.addSourceBuffer = function (mime) { const sb = new SourceBuffer(); calls.push({ m: "addSourceBuffer", self: this, args: [...arguments], ret: sb }); return sb; };
const ctx = { MediaSource, SourceBuffer, performance: { now: () => 7 }, Date: { now: () => nowMs },
  setInterval: (f) => timers.push(f), postMessage: (m) => posted.push(JSON.parse(JSON.stringify(m))), String, Math };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
vm.runInContext(src, ctx);                     // a second injection (a re-run on the same document): no double wrap
const res = {};
const ms = new MediaSource();
const sb = ms.addSourceBuffer('video/mp4; codecs="vp09.00.40.08"');
const c0 = calls[calls.length - 1];
res.add_same = c0.self === ms && c0.ret === sb && c0.args[0] === 'video/mp4; codecs="vp09.00.40.08"' && calls.length === 1;
const buf = new Uint8Array(1000);
const r = sb.appendBuffer(buf);
const c1 = calls[calls.length - 1];
res.append_same = r === SENT && c1.self === sb && c1.args.length === 1 && c1.args[0] === buf && calls.length === 2;
let thrown = null;
try { sb.remove(-1, 5); } catch (e) { thrown = e; }
res.throw_same = thrown instanceof RangeError && thrown.message === "bad range" && calls.length === 3;
const evil = {}; Object.defineProperty(evil, "byteLength", { get() { throw new Error("record fault"); } });
let r2 = null, leak = null;
try { r2 = sb.appendBuffer(evil); } catch (e) { leak = e; }
res.fault_contained = leak === null && r2 === SENT && calls[calls.length - 1].args[0] === evil;
sb.changeType('video/webm; codecs="av01.0.05M.08"');
sb.remove(0, 4);
sb.fire("updateend");
res.n_calls = calls.length;
for (const f of timers) f();
res.posted = posted;
fs.writeFileSync(outPath, JSON.stringify(res));
"""


def run(now_ms: int, block_s: int, max_recs: int = 2000) -> dict:
    node = shutil.which("node")
    assert node, "node not found: the MSE wrap test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, js, out = Path(t) / "h.js", Path(t) / "w.js", Path(t) / "out.json"
        h.write_text(HARNESS)
        js.write_text(B.MSE_WRAP_JS % {"block_s": block_s, "max": max_recs})
        subprocess.run([node, str(h), str(js), str(out), str(now_ms)], check=True)
        return json.loads(out.read_text())


def test_mse_wrap_transparent_and_recorded():
    r = run(now_ms=1_000_000, block_s=0)                                       # "1": always ON
    assert r["add_same"] and r["append_same"] and r["throw_same"] and r["fault_contained"], r
    assert r["n_calls"] == 6                                                    # each call reached its original once
    assert len(r["posted"]) == 1 and r["posted"][0]["source"] == "lvqoe-mse" and r["posted"][0]["over"] == 0
    recs = r["posted"][0]["recs"]
    assert [x["op"] for x in recs] == ["add", "app", "rm", "ct", "rm", "end"], recs
    assert recs[0]["mime"] == 'video/mp4; codecs="vp09.00.40.08"' and recs[1]["bytes"] == 1000 and recs[1]["sb"] == 1
    assert (recs[2]["s"], recs[2]["e"]) == (-1, 5) and recs[3]["mime"].startswith("video/webm")
    assert (recs[5]["bend"], recs[5]["nr"]) == (12.5, 1) and all(x["t"] == 7 for x in recs)
    capped = run(now_ms=1_000_000, block_s=0, max_recs=2)
    assert len(capped["posted"][0]["recs"]) == 2 and capped["posted"][0]["over"] == 4
    off = run(now_ms=120_000 + 5_000, block_s=60)                               # block 2 (even): OFF, wrap still in place
    assert off["add_same"] and off["append_same"] and off["throw_same"] and off["posted"] == []
    on = run(now_ms=60_000 + 5_000, block_s=60)                                 # block 1 (odd): ON
    assert len(on["posted"][0]["recs"]) == 6


def test_frame_log_arm_epoch():
    assert B.frame_log_arm(0, 123.0) is True
    assert B.frame_log_arm(60, 65.0) is True and B.frame_log_arm(60, 125.0) is False and B.frame_log_arm(60, 59.9) is False


def main() -> int:
    test_mse_wrap_transparent_and_recorded()
    test_frame_log_arm_epoch()
    print("test_mse_wrap: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
