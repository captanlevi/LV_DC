#!/usr/bin/env python3
"""t93 rows 9 / 20 (levi 00124: "the wrap passes arguments, this and return through unchanged, synchronously, plus one
push; a test asserts it"): browser.MSE_WRAP_JS under node with stub MediaSource / SourceBuffer prototypes:
  - every wrapped call reaches the original synchronously with the same this and the same argument objects, and returns
    the original's value; the original's exception reaches the caller unchanged;
  - a recording fault (a buffer whose byteLength throws) never reaches the page: the original still runs and returns;
  - records: add (mime, id), app (bytes), rm (start, end), ct (mime), end (updateend: buffered end, range count), batched
    once per 1 s as one postMessage {source: "lvqoe-mse", recs, over}; past the cap counted (over);
  - OFF blocks (epoch clock, even block) record nothing while the wrap stays installed; a second injection is a no-op;
  - levi 23:51 (2): a document that never calls addSourceBuffer still posts its install once (the per-document marker);
  - levi 22:44 (1): a SourceBuffer the page creates at document start, right after the injected script ran, is wrapped;
    the install time (inst) and the first addSourceBuffer (first: t, mime) are posted whatever the arm, once in OFF.

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
if (process.argv[5] === "noadd") {           // levi 23:51 (2): a document that never uses MSE still posts its install
  for (const f of timers) f(); for (const f of timers) f();
  fs.writeFileSync(outPath, JSON.stringify({ posted })); process.exit(0);
}
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


def run(now_ms: int, block_s: int, max_recs: int = 2000, noadd: bool = False, r2: int = int(B.MSE_WRAP_R2),
        yt: bool = False) -> dict:
    node = shutil.which("node")
    assert node, "node not found: the MSE wrap test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, js, out = Path(t) / "h.js", Path(t) / "w.js", Path(t) / "out.json"
        h.write_text(HARNESS)
        js.write_text(B.mse_wrap_js(block_s, max_recs=max_recs, r2=bool(r2), webm=yt, ykey=yt))
        subprocess.run([node, str(h), str(js), str(out), str(now_ms)] + (["noadd"] if noadd else []), check=True)
        return json.loads(out.read_text())


def test_mse_wrap_transparent_and_recorded():
    r = run(now_ms=1_000_000, block_s=0)                                       # "1": always ON
    assert r["add_same"] and r["append_same"] and r["throw_same"] and r["fault_contained"], r
    assert r["n_calls"] == 6                                                    # each call reached its original once
    assert len(r["posted"]) == 1 and r["posted"][0]["source"] == "lvqoe-mse" and r["posted"][0]["over"] == 0
    p0 = r["posted"][0]                                                         # document start: wrapped from the first add
    assert p0["inst"] == 7 and p0["first"] == {"t": 7, "mime": 'video/mp4; codecs="vp09.00.40.08"'}
    recs = r["posted"][0]["recs"]
    assert [x["op"] for x in recs] == ["add", "app", "rm", "ct", "rm", "end"], recs
    assert recs[0]["mime"] == 'video/mp4; codecs="vp09.00.40.08"' and recs[1]["bytes"] == 1000 and recs[1]["sb"] == 1
    assert (recs[2]["s"], recs[2]["e"]) == (-1, 5) and recs[3]["mime"].startswith("video/webm")
    assert (recs[5]["bend"], recs[5]["nr"]) == (12.5, 1) and all(x["t"] == 7 for x in recs)
    capped = run(now_ms=1_000_000, block_s=0, max_recs=2)
    assert len(capped["posted"][0]["recs"]) == 2 and capped["posted"][0]["over"] == 4
    off = run(now_ms=120_000 + 5_000, block_s=60)                               # block 2 (even): OFF, wrap still in place
    assert off["add_same"] and off["append_same"] and off["throw_same"]
    assert [(p["recs"], p["first"]["mime"][:9]) for p in off["posted"]] == [([], "video/mp4")]   # first only, no records
    on = run(now_ms=60_000 + 5_000, block_s=60)                                 # block 1 (odd): ON
    assert len(on["posted"][0]["recs"]) == 6
    for bs, t in ((0, 1_000_000), (60, 125_000)):                              # no MSE use, ON or OFF: one install post
        na = run(now_ms=t, block_s=bs, noadd=True)["posted"]
        assert [(p["recs"], p["inst"], p["first"]) for p in na] == [([], 7, None)], na
    # t102: the yt rows on (MSE_WRAP_WEBM / MSE_WRAP_YT_KEY) keep every call transparent and every record above; the
    # only addition on a non-WebM buffer is the batch's ms record
    on_yt = run(now_ms=1_000_000, block_s=0, yt=True)
    assert on_yt["add_same"] and on_yt["append_same"] and on_yt["throw_same"] and on_yt["fault_contained"]
    assert on_yt["n_calls"] == 6
    yrecs = on_yt["posted"][0]["recs"]
    new = lambda x: {k: v for k, v in x.items() if k not in ("mode", "br") and not (x["op"] == "end" and k == "tso")}
    assert [new(x) for x in yrecs if x["op"] != "ms"] == recs and [x["op"] for x in yrecs][-1] == "ms", yrecs


def test_frame_log_arm_epoch():
    assert B.frame_log_arm(0, 123.0) is True
    assert B.frame_log_arm(60, 65.0) is True and B.frame_log_arm(60, 125.0) is False and B.frame_log_arm(60, 59.9) is False


# t100 R2 (rushi 14:35; levi 14:35: "off" counts only if the off-state records are byte-identical to 971e444e's): a plan
# harness (appends of real fMP4 / WebM heads, a tso set, garbage) run on the frozen 971e444e wrap (fixtures/) and on the
# current wrap with R2 off (identical posts) and on (tso, tf / ctc, init / tcs; garbage: tf null or absent, never a throw).
R2_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [srcPath, planPath, outPath] = process.argv.slice(2), src = fs.readFileSync(srcPath, "utf8");
const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
const posted = [], timers = [], calls = [], SENT = { ret: 1 };
class SourceBuffer { constructor() { this.ls = {}; this._tso = 0; this.buffered = { length: 1, end: () => 3.5 }; }
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); } fire(t) { for (const f of this.ls[t] || []) f(); } }
Object.defineProperty(SourceBuffer.prototype, "timestampOffset", { configurable: true, get() { return this._tso; }, set(v) { this._tso = v; } });
SourceBuffer.prototype.appendBuffer = function (b) { calls.push(b); return SENT; };
SourceBuffer.prototype.remove = function () {}; SourceBuffer.prototype.changeType = function () {};
class MediaSource {} MediaSource.prototype.addSourceBuffer = function () { return new SourceBuffer(); };
const ctx = { MediaSource, SourceBuffer, performance: { now: () => 7 }, Date: { now: () => plan.now_ms }, String, Math,
  Uint8Array, DataView, ArrayBuffer, setInterval: (f) => timers.push(f), postMessage: (m) => posted.push(JSON.parse(JSON.stringify(m))) };
ctx.window = ctx; vm.createContext(ctx); vm.runInContext(src, ctx);
const sb = new MediaSource().addSourceBuffer(plan.mime); let leaks = 0, rets = 0;
for (const st of plan.steps) { if (st.tso !== undefined) sb.timestampOffset = st.tso;
  try { rets += sb.appendBuffer(Uint8Array.from(Buffer.from(st.hex, "hex"))) === SENT ? 1 : 0; } catch (e) { leaks += 1; } }
sb.fire("updateend"); for (const f of timers) f();
fs.writeFileSync(outPath, JSON.stringify({ posted, leaks, rets, calls: calls.length }));
"""


def run_plan(src: str, steps: list, mime: str, now_ms: int = 1_000_000) -> dict:
    node = shutil.which("node")
    assert node, "node not found: the MSE wrap test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, js, plan, out = (Path(t) / x for x in ("h.js", "w.js", "plan.json", "out.json"))
        h.write_text(R2_HARNESS)
        js.write_text(src)
        plan.write_text(json.dumps({"now_ms": now_ms, "mime": mime, "steps": steps}))
        subprocess.run([node, str(h), str(js), str(plan), str(out)], check=True)
        return json.loads(out.read_text())


def _ebml(id_: bytes, payload: bytes) -> bytes:
    n = len(payload)
    return id_ + (bytes([0x80 | n]) if n < 127 else bytes([0x40 | (n >> 8), n & 0xFF])) + payload


def r2_plans():
    import test_worker_anchor as W
    mp4 = [{"hex": W.init_seg(W.avc1(1280, 720)).hex()}, {"hex": W.frag(1, 900_000, [bytes(20)], 33000).hex(), "tso": -2.5},
           {"hex": "00000010ffffffff" + "00" * 40}, {"hex": "0000000c" + "6d6f6f66" + "00000000"},   # garbage, a short moof
           {"hex": "ff" * 3}]
    tcs = _ebml(b"\x2a\xd7\xb1", (1_000_000).to_bytes(3, "big"))
    webm_init = _ebml(b"\x1a\x45\xdf\xa3", b"\x42\x86\x81\x01") + _ebml(b"\x18\x53\x80\x67", _ebml(b"\x15\x49\xa9\x66", tcs))
    cluster = _ebml(b"\x1f\x43\xb6\x75", _ebml(b"\xe7", (123456).to_bytes(3, "big")) + _ebml(b"\xa3", bytes(30)))
    webm = [{"hex": webm_init.hex()}, {"hex": cluster.hex()}, {"hex": "1f43b675" + "ff"}]
    return [('video/mp4; codecs="avc1.4D401F"', mp4), ('video/webm; codecs="vp09.00.40.08"', webm)]


def test_r2_off_is_971e444e_and_on_reads_heads():
    old = (HERE / "fixtures" / "mse_wrap_js_971e444e.js").read_text() % {"block_s": 0, "max": 2000}
    for mime, steps in r2_plans():
        o = run_plan(old, steps, mime)
        off = run_plan(B.mse_wrap_js(0, max_recs=2000, r2=False, webm=False, ykey=False), steps, mime)
        assert json.dumps(off, sort_keys=True) == json.dumps(o, sort_keys=True), (mime, off, o)
        on = run_plan(B.mse_wrap_js(0, max_recs=2000, r2=True), steps, mime)
        assert on["leaks"] == 0 and on["rets"] == len(steps) and on["calls"] == len(steps)
        recs = on["posted"][0]["recs"]
        apps = [r for r in recs if r["op"] == "app"]
        assert [r["bytes"] for r in apps] == [r["bytes"] for r in o["posted"][0]["recs"] if r["op"] == "app"]
        if "mp4" in mime:
            assert [r for r in recs if r["op"] == "init"][0]["tr"] == [[1, 1_000_000, "vide"], [2, 48000, "soun"]]
            assert apps[1]["tf"] == [1, 900_000] and apps[1]["tso"] == -2.5
            assert apps[0].get("tf") is None and "r2err" not in apps[0]                       # an init: no moof
            assert all("tf" not in a or a["tf"] is None for a in apps[2:]), apps[2:]          # garbage: null / absent
        else:
            assert [r["v"] for r in recs if r["op"] == "tcs"] == [1_000_000]
            assert apps[1]["ctc"] == 123456 and "ctc" not in apps[2] and "ctc" not in apps[0]
    # null: the fixture is 971e444e's wrap, not the current one (the identity above is not a file compared with itself)
    assert "R2" not in (HERE / "fixtures" / "mse_wrap_js_971e444e.js").read_text() and "R2 = %(r2)d" in B.MSE_WRAP_JS


def main() -> int:
    test_mse_wrap_transparent_and_recorded()
    test_frame_log_arm_epoch()
    test_r2_off_is_971e444e_and_on_reads_heads()
    print("test_mse_wrap: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
