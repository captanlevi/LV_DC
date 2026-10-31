#!/usr/bin/env python3
"""tt candidate W (rushi 11:3x reopen; levi 00153 gate, B): the no-pause worker attach + full's install + a WebCodecs block.

  - the attach: WORKER_ANCHOR_ATTACH_W = WORKER_ANCHOR_ATTACH with waitForDebuggerOnStart False (the laptop screen: the
    pause alone flips tt to MediaSourceHandle); W's WorkerAnchor arms it and records it; full / proxy / b1 keep the pause;
    mutant: a pause under W is refused (ValueError at construction);
  - the script: full's, byte for byte, plus ANCHOR_W_EDITS; full / proxy carry no WebCodecs block;
  - under node (no MediaSource in the stub: the MSE part stays inert): configure -> wcfg (codec, coded size, description
    bytes), decode -> wdec (chunk ts in us, ty, n; k = FNV-1a of the first / last 256 bytes on a key and every
    KEY_EVERY-th chunk), reset -> wrst, a VideoFrame written to a stream writer -> wout (its ts); every wrapped call
    passes its this / arguments / return through, a fault in the record is contained, the take carries nd;
  - refusals: W in LVDC_ANCHOR_AB_TW (or TW_WORKER_ANCHOR = W) is refused (tt only); the tt cycle off,W with the frame log
    off is refused (the install needs the frame log); off,W with it on is accepted.

    python3 test_anchor_w.py        (needs node; exit 0 = pass)
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
const [srcPath, outPath] = process.argv.slice(2), src = fs.readFileSync(srcPath, "utf8");
const calls = [], RET = { ret: "write-return" };
class VideoDecoder {}
VideoDecoder.prototype.configure = function (c) { calls.push({ m: "configure", self: this, args: [...arguments] }); };
VideoDecoder.prototype.decode = function (ch) { calls.push({ m: "decode", self: this, args: [...arguments] }); return undefined; };
VideoDecoder.prototype.reset = function () { calls.push({ m: "reset", self: this }); };
class VideoFrame { constructor(ts) { this.timestamp = ts; } }
class WritableStreamDefaultWriter {}
WritableStreamDefaultWriter.prototype.write = function (x) { calls.push({ m: "write", self: this, args: [...arguments] }); return RET; };
const chunk = (ts, type, bytes) => ({ timestamp: ts, type, byteLength: bytes.length, copyTo(dst) { dst.set(bytes); } });
let now = 5;
const ctx = { VideoDecoder, VideoFrame, WritableStreamDefaultWriter, performance: { now: () => now, timeOrigin: 1761740000123.5 },
  Date: { now: () => 1000000 }, JSON, Math, String, Uint8Array, DataView, ArrayBuffer, WeakMap, Object, RangeError, Error };
ctx.self = ctx;
vm.createContext(ctx);
const out = { first: vm.runInContext(src, ctx), second: vm.runInContext(src, ctx), checks: {} };
const d = new VideoDecoder(), w = new WritableStreamDefaultWriter();
const big = Uint8Array.from({ length: 1000 }, (_, i) => (i * 7 + 3) & 255), small = Uint8Array.from([1, 2, 3, 4, 5]);
const cfg = { codec: "hev1.1.6.L120.90", codedWidth: 720, codedHeight: 1280, description: new Uint8Array(23) };
d.configure(cfg); out.checks.cfg_same = calls[0].self === d && calls[0].args[0] === cfg && calls[0].args.length === 1;
const k0 = chunk(40000, "key", big); d.decode(k0); out.checks.dec_same = calls[1].self === d && calls[1].args[0] === k0;
for (let i = 1; i < 30; i++) { now += 1; d.decode(chunk(40000 + i * 40000, "delta", small)); }   // the 30th (nd 30) is keyed
const evil = {}; Object.defineProperty(evil, "timestamp", { get() { throw new Error("fault"); } });
let leak = null; try { d.decode(evil); } catch (e) { leak = e; }
out.checks.fault_contained = leak === null && calls[calls.length - 1].args[0] === evil;
const f = new VideoFrame(80000); const r1 = w.write(f); out.checks.write_same = r1 === RET && calls[calls.length - 1].args[0] === f;
const r2 = w.write("not a frame"); out.checks.write_other = r2 === RET;
d.reset();
out.n_calls = calls.length;
out.take = JSON.parse(ctx.__lvqoeAnchorTake());
fs.writeFileSync(outPath, JSON.stringify(out));
"""


def fnv(b: bytes) -> int:
    h = 0x811c9dc5
    for x in b:
        h = ((h ^ x) * 0x01000193) & 0xffffffff
    return h


def run_w() -> dict:
    node = shutil.which("node")
    assert node, "node not found: the W test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, src, out = (Path(t) / x for x in ("h.js", "w.js", "out.json"))
        h.write_text(HARNESS)
        src.write_text(B.worker_anchor_js(0, w=True))
        subprocess.run([node, str(h), str(src), str(out)], check=True)
        return json.loads(out.read_text())


def test_w_script_is_full_plus_edits():
    full, w = B.worker_anchor_js(0), B.worker_anchor_js(0, w=True)
    assert "VideoDecoder" not in full and "VideoDecoder" not in B.worker_anchor_js(0, proxy=True)
    x = full
    for old, new in B.ANCHOR_W_EDITS:
        assert x.count(old) == 1
        x = x.replace(old, new)
    assert x == w and "wdec" in w and "nv, nd, to:" in w


def test_w_records_under_node():
    r = run_w()
    assert r["first"] == "ok" and r["second"] == "again", r
    assert all(r["checks"].values()) and len(r["checks"]) == 5, r["checks"]
    recs = r["take"]["recs"]
    cfg = [x for x in recs if x["op"] == "wcfg"]
    assert cfg == [{"op": "wcfg", "dec": 1, "codec": "hev1.1.6.L120.90", "w": 720, "h": 1280, "desc": 23, "t": cfg[0]["t"]}]
    dec = [x for x in recs if x["op"] == "wdec"]
    assert len(dec) == 30 and r["take"]["nd"] == 30, (len(dec), r["take"]["nd"])   # the faulting chunk: counted out, contained
    big = bytes(((i * 7 + 3) & 255) for i in range(1000))
    assert dec[0]["ts"] == 40000 and dec[0]["ty"] == 1 and dec[0]["n"] == 1000
    assert dec[0]["k"] == [fnv(big[:256]), fnv(big[-256:])]
    assert all("k" not in x for x in dec[1:29]) and dec[29]["k"] == [fnv(bytes([1, 2, 3, 4, 5]))] * 2
    assert [x["ts"] for x in dec] == [40000 * (i + 1) for i in range(30)]
    assert [x for x in recs if x["op"] == "wout"] == [{"op": "wout", "ts": 80000, "t": [x for x in recs if x["op"] == "wout"][0]["t"]}]
    assert [x["op"] for x in recs if x["op"] == "wrst"] == ["wrst"]
    assert [x["m"] for x in recs if x["op"] == "err"] == ["Error: fault"], recs        # full's timed(): logged, contained


def test_w_attach_no_pause_and_recorded():
    assert B.WORKER_ANCHOR_ATTACH_W == {**B.WORKER_ANCHOR_ATTACH, "waitForDebuggerOnStart": False}
    assert B.WORKER_ANCHOR_ATTACH["waitForDebuggerOnStart"] is True
    a = B.WorkerAnchor("ws://x", 0, "W")
    assert a.attach is B.WORKER_ANCHOR_ATTACH_W and a.record()["attach"]["waitForDebuggerOnStart"] is False
    assert a.js == B.worker_anchor_js(0, w=True)
    for arm in ("full", "proxy", "b1"):
        assert B.WorkerAnchor("ws://x", 0, arm).attach is B.WORKER_ANCHOR_ATTACH


def test_w_pause_refused():
    keep = B.WORKER_ANCHOR_ATTACH_W
    try:
        B.WORKER_ANCHOR_ATTACH_W = {**keep, "waitForDebuggerOnStart": True}
        try:
            B.WorkerAnchor("ws://x", 0, "W")
        except ValueError:
            pass
        else:
            raise AssertionError("a pause under W was accepted")
    finally:
        B.WORKER_ANCHOR_ATTACH_W = keep


def test_w_refusals():
    on = {"LVQOE_FRAME_LOG": "1"}
    assert B.anchor_ab_refusal({**on, "LVDC_ANCHOR_AB_TT": "off,W,W"}, False) is None
    assert "tt only" in B.anchor_ab_refusal({**on, "LVDC_ANCHOR_AB_TW": "off,W"}, False)
    assert "frame log off" in B.anchor_ab_refusal({"LVDC_ANCHOR_AB_TT": "off,W"}, False)
    assert "counted run" in B.anchor_ab_refusal({**on, "LVDC_ANCHOR_AB_TT": "off,W"}, True)
    keep = B.TW_WORKER_ANCHOR
    try:
        B.TW_WORKER_ANCHOR = "W"
        assert "tt only" in B.anchor_ab_refusal({}, False)
    finally:
        B.TW_WORKER_ANCHOR = keep
    assert B.anchor_arm("tiktok", {**on, "LVDC_ANCHOR_AB_TT": "off,W,W"}, "ti_100001", False)["arm"] == "W"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted((k, v) for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            fails += 1
            print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if fails else 0)
