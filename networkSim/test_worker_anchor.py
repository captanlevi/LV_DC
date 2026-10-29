#!/usr/bin/env python3
"""t100 R1 (levi b3_anchor_rows_gates 14:28 + 14:30 / 14:32 amendments): the tw / tt worker MSE anchor.

WORKER_ANCHOR_JS under node, stub MediaSource / SourceBuffer (timestampOffset a real accessor), synthetic fMP4 built here:
  - every wrapped call reaches the original with the same this and arguments and returns its value; the original's
    exception reaches the caller; a recording fault (a buffer whose bytes cannot be read) never does;
  - init: per track [tid, timescale, handler, sample entry, width, height, codec]; app (video) per append, every append:
    tso in force + per video traf [tid, tfdt, cto0, n, sum dur]; aapp (audio) the same, no keys; end: buffered ranges;
  - keys: every 30th video sample by count + the first after init / abort / changeType / tso set / a tfdt discontinuity;
    the key equals a Python reference of brad's spec (VCL NALs H.264 1-5 / HEVC (b0 >> 1) & 0x3f < 32, prefixes dropped,
    FNV-1a 32 over VCL[0:256] + VCL[max(256, L-256):L], an integer) on AVC and HEVC samples; null: hashing every byte
    differs on long samples;
  - OFF blocks: no app / aapp / end, the state records (add / init / tso / abort / ct / rm) still; the cap counts over;
  - garbage appends: no throw, no app traf, the original still runs.
WorkerAnchor.handle with a fake send (no browser): levi's hard requirement, the resume: runIfWaitingForDebugger goes to
every attached target in the same call as the install, also when the install's send raises and before any reply (an
install that hangs); the install carries the 50 ms timeout; the relay into the page; detach; the coverage record.

    python3 test_worker_anchor.py        (needs node; exit 0 = pass)
"""
import json
import random
import shutil
import struct
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


# ---- synthetic fMP4 -------------------------------------------------------------------------------------------------
def box(t: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + t + payload


def full(t: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(t, bytes([version]) + flags.to_bytes(3, "big") + payload)


def trak(tid: int, timescale: int, handler: bytes, entry: bytes) -> bytes:
    tkhd = full(b"tkhd", 0, 3, struct.pack(">III", 0, 0, tid) + bytes(68))
    mdhd = full(b"mdhd", 0, 0, struct.pack(">IIII", 0, 0, timescale, 0) + bytes(4))
    hdlr = full(b"hdlr", 0, 0, bytes(4) + handler + bytes(12) + b"\0")
    stsd = full(b"stsd", 0, 0, struct.pack(">I", 1) + entry)
    return box(b"trak", tkhd + box(b"mdia", mdhd + hdlr + box(b"minf", box(b"stbl", stsd))))


def avc1(w: int, h: int) -> bytes:
    visual = bytes(6) + struct.pack(">H", 1) + bytes(16) + struct.pack(">HH", w, h) + bytes(50)
    return box(b"avc1", visual + box(b"avcC", bytes([1, 0x4D, 0x40, 0x1F]) + bytes(3)))


def hev1(w: int, h: int) -> bytes:
    return box(b"hev1", bytes(6) + struct.pack(">H", 1) + bytes(16) + struct.pack(">HH", w, h) + bytes(50))


def init_seg(video_entry: bytes, v_ts: int = 1_000_000, with_audio: bool = True) -> bytes:
    traks = trak(1, v_ts, b"vide", video_entry) + (trak(2, 48000, b"soun", box(b"mp4a", bytes(28))) if with_audio else b"")
    return box(b"ftyp", b"isom" + bytes(4)) + box(b"moov", traks)


def frag(tid: int, bdt: int, samples: list, dur: int, ctos: list | None = None) -> bytes:
    """One moof (one traf, default-base-is-moof, trun data offset / duration / size / cto) + its mdat."""
    n = len(samples)
    ctos = ctos or [0] * n

    def moof(data_offset: int) -> bytes:
        tfhd = full(b"tfhd", 0, 0x20000, struct.pack(">I", tid))
        tfdt = full(b"tfdt", 1, 0, struct.pack(">Q", bdt))
        body = struct.pack(">Ii", n, data_offset) + b"".join(struct.pack(">IIi", dur, len(s), c) for s, c in zip(samples, ctos))
        trun = full(b"trun", 1, 0x1 | 0x100 | 0x200 | 0x800, body)
        return box(b"moof", full(b"mfhd", 0, 0, struct.pack(">I", 1)) + box(b"traf", tfhd + tfdt + trun))
    size = len(moof(0))
    return moof(size + 8) + box(b"mdat", b"".join(samples))


def nal(t: int, n: int, hevc: bool, rnd: random.Random) -> bytes:
    head = bytes([(t & 0x3F) << 1, 1]) if hevc else bytes([0x60 | t])
    return head + bytes(rnd.getrandbits(8) for _ in range(max(0, n - len(head))))


def sample(nals: list) -> bytes:
    return b"".join(struct.pack(">I", len(x)) + x for x in nals)


def fnv(b: bytes) -> int:
    h = 0x811C9DC5
    for x in b:
        h = ((h ^ x) * 0x01000193) & 0xFFFFFFFF
    return h


def ref_key(nals: list, hevc: bool) -> list:
    vcl = b"".join(x for x in nals if (((x[0] >> 1) & 0x3F) < 32 if hevc else 1 <= (x[0] & 0x1F) <= 5))
    L = len(vcl)
    return [L, fnv(vcl[:256] + vcl[max(256, L - 256):]), 1]


# ---- node harness ---------------------------------------------------------------------------------------------------
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [srcPath, planPath, outPath] = process.argv.slice(2), src = fs.readFileSync(srcPath, "utf8");
const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
const calls = [], SENT = { ret: "append-return" };
class SourceBuffer {
  constructor() { this.ls = {}; this._tso = 0; this._ranges = [[0, 1.5]]; }
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); }
  fire(t) { for (const f of this.ls[t] || []) f(); }
  get buffered() { const r = this._ranges; return { length: r.length, start: (i) => r[i][0], end: (i) => r[i][1] }; }
}
Object.defineProperty(SourceBuffer.prototype, "timestampOffset", { configurable: true, enumerable: true,
  get() { return this._tso; }, set(v) { calls.push({ m: "tso=", v }); this._tso = v; } });
SourceBuffer.prototype.appendBuffer = function (buf) { calls.push({ m: "appendBuffer", self: this, args: [...arguments] }); return SENT; };
SourceBuffer.prototype.remove = function (s, e) { calls.push({ m: "remove", self: this, args: [...arguments] }); if (s < 0) throw new RangeError("bad range"); };
SourceBuffer.prototype.abort = function () { calls.push({ m: "abort", self: this }); };
SourceBuffer.prototype.changeType = function (t) { calls.push({ m: "changeType", self: this, args: [...arguments] }); };
class MediaSource {}
MediaSource.prototype.addSourceBuffer = function (mime) { const sb = new SourceBuffer(); calls.push({ m: "addSourceBuffer", self: this, ret: sb }); return sb; };
let now = 5;
const ctx = { MediaSource, SourceBuffer, performance: { now: () => now, timeOrigin: 1761740000123.5 }, Date: { now: () => plan.now_ms }, JSON, Math, String,
  Uint8Array, DataView, ArrayBuffer, WeakMap, Object, RangeError, Error };
ctx.self = ctx;
vm.createContext(ctx);
const out = { first: vm.runInContext(src, ctx), second: vm.runInContext(src, ctx), checks: {} };
const ms = new MediaSource(), sbs = [];
const hex = (h) => Uint8Array.from(Buffer.from(h, "hex"));
for (const st of plan.steps) {
  now += 1;
  if (st.op === "add") { const sb = ms.addSourceBuffer(st.mime); sbs.push(sb); out.checks.add_same = calls[calls.length - 1].ret === sb; }
  else if (st.op === "app") { const sb = sbs[st.sb], b = hex(st.hex); const r = sb.appendBuffer(b); const c = calls[calls.length - 1];
    out.checks.append_same = (out.checks.append_same ?? true) && r === SENT && c.self === sb && c.args[0] === b && c.args.length === 1; }
  else if (st.op === "evil") { const evil = {}; Object.defineProperty(evil, "byteLength", { get() { throw new Error("fault"); } });
    Object.defineProperty(evil, "buffer", { get() { throw new Error("fault"); } });
    let leak = null, r = null; try { r = sbs[st.sb].appendBuffer(evil); } catch (e) { leak = e; }
    out.checks.fault_contained = leak === null && r === SENT; }
  else if (st.op === "tso") { sbs[st.sb].timestampOffset = st.v; out.checks.tso_set = sbs[st.sb].timestampOffset === st.v; }
  else if (st.op === "abort") sbs[st.sb].abort();
  else if (st.op === "ct") sbs[st.sb].changeType(st.mime);
  else if (st.op === "rm") { let th = null; try { sbs[st.sb].remove(st.s, st.e); } catch (e) { th = e; }
    if (st.s < 0) out.checks.throw_same = th instanceof RangeError && th.message === "bad range"; }
  else if (st.op === "end") { sbs[st.sb]._ranges = st.ranges; sbs[st.sb].fire("updateend"); }
}
out.n_calls = calls.length;
out.take = JSON.parse(ctx.__lvqoeAnchorTake());
out.take2 = JSON.parse(ctx.__lvqoeAnchorTake());
fs.writeFileSync(outPath, JSON.stringify(out));
"""


def run_js(steps: list, now_ms: int = 1_000_000, block_s: int = 0, js: str | None = None) -> dict:
    node = shutil.which("node")
    assert node, "node not found: the worker anchor test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, src, plan, out = (Path(t) / x for x in ("h.js", "w.js", "plan.json", "out.json"))
        h.write_text(HARNESS)
        src.write_text(js if js is not None else B.worker_anchor_js(block_s))
        plan.write_text(json.dumps({"now_ms": now_ms, "steps": steps}))
        subprocess.run([node, str(h), str(src), str(plan), str(out)], check=True)
        return json.loads(out.read_text())


def recs_of(r: dict, op: str) -> list:
    return [x for x in r["take"]["recs"] if x["op"] == op]


def test_worker_wrap_transparent_records_and_keys():
    rnd = random.Random(7)
    v_samples, v_nals = [], []
    for i in range(70):
        ns = [nal(9, 2, False, rnd), nal(6, 20, False, rnd), nal(5 if i == 0 else 1, rnd.choice([40, 300, 700, 2000]), False, rnd)]
        v_nals.append(ns)
        v_samples.append(sample(ns))
    a_frag = frag(2, 0, [bytes(10)] * 3, 1024)
    steps = [{"op": "add", "mime": 'video/mp4;codecs="avc1.4D401F"'}, {"op": "add", "mime": 'audio/mp4;codecs="mp4a.40.2"'},
             {"op": "app", "sb": 0, "hex": init_seg(avc1(1280, 720), with_audio=False).hex()},
             {"op": "app", "sb": 1, "hex": (init_seg(avc1(1280, 720)) + a_frag).hex()}]
    for i in range(70):                                         # per-frame appends, as tw's TS path: one sample per moof
        bdt = i * 33000 + (500_000 if i >= 50 else 0)           # a tfdt discontinuity at sample 50
        steps.append({"op": "app", "sb": 0, "hex": frag(1, bdt, [v_samples[i]], 33000, [1000]).hex()})
        if i == 40:
            steps.append({"op": "tso", "sb": 0, "v": -12.5})
    steps += [{"op": "abort", "sb": 0}, {"op": "app", "sb": 0, "hex": frag(1, 70 * 33000 + 500_000, [v_samples[0]], 33000).hex()},
              {"op": "rm", "sb": 0, "s": -1, "e": 5}, {"op": "rm", "sb": 0, "s": 0, "e": 4},
              {"op": "ct", "sb": 0, "mime": 'video/mp4;codecs="hev1.1.6.L93.B0"'},
              {"op": "end", "sb": 0, "ranges": [[0, 1.0], [2.0, 3.5]]}, {"op": "evil", "sb": 0},
              {"op": "app", "sb": 0, "hex": "00000010" + "ffffffff" + "00" * 40}]          # garbage: a box past the end
    r = run_js(steps)
    assert r["first"] == "ok" and r["second"] == "again", r
    assert r["checks"] == {"add_same": True, "append_same": True, "tso_set": True, "throw_same": True,
                           "fault_contained": True}, r["checks"]
    inits = recs_of(r, "init")
    assert inits[0]["tr"] == [[1, 1_000_000, "vide", "avc1", 1280, 720, "avc1.4D401F"]], inits
    assert inits[1]["tr"][1][:4] == [2, 48000, "soun", "mp4a"], inits
    apps = recs_of(r, "app")
    assert len(apps) == 71 and all(len(a["v"]) == 1 and a["sb"] == 1 for a in apps), len(apps)
    assert apps[0]["v"] == [[1, 0, 1000, 1, 33000]] and apps[0]["tso"] == 0 and apps[45]["tso"] == -12.5
    aapp = recs_of(r, "aapp")
    assert len(aapp) == 1 and aapp[0]["v"] == [[2, 0, 0, 3, 3072]] and "key" not in aapp[0], aapp
    keyed = {i: a["key"] for i, a in enumerate(apps) if "key" in a}
    # first after init (0), count 30 / 60, after the tso set (41), the discontinuity (50), after the abort (70)
    assert sorted(keyed) == [0, 29, 41, 50, 59, 70], sorted(keyed)
    for i, ks in keyed.items():
        src_i = 0 if i == 70 else i
        assert len(ks) == 1 and ks[0][:3] == [1, apps[i]["v"][0][1], 1000 if i < 70 else 0], (i, ks)
        assert ks[0][3:] == ref_key(v_nals[src_i], False), (i, ks[0], ref_key(v_nals[src_i], False))
    assert recs_of(r, "tso") == [{"op": "tso", "sb": 1, "v": -12.5, "was": 0, "t": recs_of(r, "tso")[0]["t"]}]
    assert [x["tso"] for x in recs_of(r, "abort")] == [-12.5]
    assert [(x["s"], x["e"]) for x in recs_of(r, "rm")] == [(-1, 5), (0, 4)]
    assert recs_of(r, "ct")[0]["mime"].startswith("video/mp4;codecs=\"hev1")
    assert [(x["br"], x["nr"]) for x in recs_of(r, "end")] == [([[0, 1.0], [2.0, 3.5]], 2)]
    # every call reached its original once: 2 adds, 2 inits, 70 + 1 fragments, the tso set, abort, 2 removes, changeType,
    # the faulting buffer, the garbage (updateend is an event, not a call); the garbage made no app record (71 above)
    assert r["n_calls"] == 2 + 2 + 71 + 1 + 1 + 2 + 1 + 1 + 1
    assert r["take"]["over"] == 0 and r["take"]["nv"] == 71 and r["take2"]["recs"] == []
    # levi 14:59: the worker's clock is named: t + to = epoch ms
    assert r["take"]["to"] == 1761740000123.5 and abs(apps[0]["t"] + r["take"]["to"] - 1761740000123.5 - apps[0]["t"]) < 1e-6
    assert r["take"]["first"]["mime"].startswith("video/mp4")


def test_worker_key_matches_reference_avc_and_hevc():
    rnd = random.Random(11)
    for hevc in (False, True):
        mime = 'video/mp4;codecs="hev1.1.6.L93.B0"' if hevc else 'video/mp4;codecs="avc1.4D401F"'
        steps, refs = [{"op": "add", "mime": mime},
                       {"op": "app", "sb": 0, "hex": init_seg(hev1(720, 1280) if hevc else avc1(640, 360), with_audio=False).hex()}], []
        for i in range(60):
            if hevc:   # VCL 0-31 (trail 1, idr 19, cra 21), non-VCL vps 32 / sps 33 / pps 34 / sei 39 / aud 35
                types_ = [rnd.choice([35, 32, 33, 34, 39])] + [rnd.choice([1, 19, 21, 0, 31])] * rnd.randint(1, 3)
            else:
                types_ = [rnd.choice([9, 6, 7, 8])] + [rnd.choice([1, 5])] * rnd.randint(1, 3)
            ns = [nal(t, rnd.choice([3, 100, 255, 256, 257, 511, 600, 5000]), hevc, rnd) for t in types_]
            refs.append(ref_key(ns, hevc))
            steps.append({"op": "app", "sb": 0, "hex": frag(1, i * 40, [sample(ns)], 40).hex()})
        r = run_js(steps)
        apps = recs_of(r, "app")
        keyed = [(i, a["key"][0]) for i, a in enumerate(apps) if "key" in a]
        assert [i for i, _ in keyed] == [0, 29, 59], keyed
        for i, k in keyed:
            assert k[3:] == refs[i], (hevc, i, k, refs[i])
    # null: a hash over every VCL byte differs from the key on samples longer than 512 B
    long_ = [nal(1, 5000, False, random.Random(3))]
    vcl = long_[0]
    assert fnv(vcl) != ref_key(long_, False)[1]


def test_worker_off_block_keeps_state_and_cap_counts():
    rnd = random.Random(5)
    s = sample([nal(5, 300, False, rnd)])
    steps = [{"op": "add", "mime": 'video/mp4;codecs="avc1.4D401F"'},
             {"op": "app", "sb": 0, "hex": init_seg(avc1(640, 360), with_audio=False).hex()},
             {"op": "app", "sb": 0, "hex": frag(1, 0, [s], 33000).hex()}, {"op": "tso", "sb": 0, "v": 3.0},
             {"op": "abort", "sb": 0}, {"op": "rm", "sb": 0, "s": 0, "e": 1}, {"op": "end", "sb": 0, "ranges": [[0, 1]]},
             {"op": "ct", "sb": 0, "mime": 'video/mp4;codecs="avc1.64001F"'}]
    off = run_js(steps, now_ms=120_000 + 5_000, block_s=60)        # block 2 (even): OFF
    assert sorted({x["op"] for x in off["take"]["recs"]}) == ["abort", "add", "ct", "init", "rm", "tso"], off["take"]
    on = run_js(steps, now_ms=60_000 + 5_000, block_s=60)           # block 1 (odd): ON
    assert sorted({x["op"] for x in on["take"]["recs"]}) == ["abort", "add", "app", "ct", "end", "init", "rm", "tso"]
    capped = run_js(steps, js=B.worker_anchor_js(0).replace("MAX = %d" % B.FRAME_LOG_MSE_MAX, "MAX = 3"))
    assert len(capped["take"]["recs"]) == 3 and capped["take"]["over"] == 5, capped["take"]


# ---- WorkerAnchor.handle (no browser) -------------------------------------------------------------------------------
class FakeSend:
    def __init__(self, fail_on: str | None = None):
        self.sent, self.fail_on, self.i = [], fail_on, 0

    def __call__(self, method, params, sid=None):
        if method == self.fail_on:
            raise RuntimeError("socket gone")
        self.i += 1
        self.sent.append((self.i, method, params, sid))
        return self.i


def attach(sid="S1", ttype="worker", url="blob:https://www.twitch.tv/x", waiting=True):
    return {"method": "Target.attachedToTarget", "params": {"sessionId": sid, "waitingForDebugger": waiting,
            "targetInfo": {"targetId": "T" + sid, "type": ttype, "url": url}}}


def test_attach_filter_and_resume_always():
    assert B.WORKER_ANCHOR_ATTACH["filter"] == [{"type": "worker", "exclude": False}, {"exclude": True}]
    assert B.WORKER_ANCHOR_ATTACH["waitForDebuggerOnStart"] is True and B.WORKER_ANCHOR_ATTACH["flatten"] is True
    a, s = B.WorkerAnchor("ws://x", 0), FakeSend()
    a.handle(attach(), s, now=100.0)
    assert [(m, sid) for _, m, _, sid in s.sent] == [("Runtime.evaluate", "S1"), ("Runtime.runIfWaitingForDebugger", "S1")]
    assert s.sent[0][2]["timeout"] == 50 and "__lvqoeAnchor" in s.sent[0][2]["expression"]
    # the install "hangs" (never answered): the resume went out already, in the same call
    assert a.workers[0]["resume_sent_ms"] is not None and a.workers[0]["wrap"] is None
    a.handle({"id": 2, "result": {}}, s, now=100.004)                                   # the resume's reply
    assert a.workers[0]["resume_ms"] == 4.0
    # the install's send raises: the worker is still resumed
    a2, s2 = B.WorkerAnchor("ws://x", 0), FakeSend(fail_on="Runtime.evaluate")
    a2.handle(attach(), s2, now=1.0)
    assert [m for _, m, _, _ in s2.sent] == ["Runtime.runIfWaitingForDebugger"] and "socket gone" in a2.workers[0]["wrap_error"]
    # the install throws in the worker: recorded, resumed
    a3, s3 = B.WorkerAnchor("ws://x", 0), FakeSend()
    a3.handle(attach(), s3, now=1.0)
    a3.handle({"id": 1, "result": {"result": {"type": "object"}, "exceptionDetails": {"text": "Uncaught"}}}, s3, now=1.01)
    assert a3.workers[0]["wrap"] is None and "Uncaught" in a3.workers[0]["wrap_error"]
    assert [m for _, m, _, _ in s3.sent] == ["Runtime.evaluate", "Runtime.runIfWaitingForDebugger"]
    # a target that is not a dedicated worker (never expected under the filter): resumed, nothing installed
    a4, s4 = B.WorkerAnchor("ws://x", 0), FakeSend()
    a4.handle(attach(ttype="iframe", url="https://x"), s4, now=1.0)
    assert [m for _, m, _, _ in s4.sent] == ["Runtime.runIfWaitingForDebugger"] and a4._live == {}


def test_relay_detach_and_record():
    a, s = B.WorkerAnchor("ws://x", 0), FakeSend()
    a.handle(attach("S1"), s, now=10.0)
    a.handle({"id": 1, "result": {"result": {"type": "string", "value": "ok"}}}, s, now=10.01)
    a.take(s)
    tid = s.sent[-1][0]
    assert s.sent[-1][1:] == ("Runtime.evaluate", {"expression": "self.__lvqoeAnchorTake ? self.__lvqoeAnchorTake() : null",
                                                   "returnByValue": True}, "S1")
    batch = {"recs": [{"op": "app", "sb": 1, "v": [[1, 0, 0, 1, 33]], "t": 1}, {"op": "tso", "sb": 1, "v": 2, "was": 0, "t": 2}],
             "over": 0, "inst": 3.5, "first": {"t": 4, "mime": "video/mp4"}, "ms": 0.4, "nv": 1, "to": 1761740000000.25}
    a.handle({"id": tid, "result": {"result": {"type": "string", "value": json.dumps(batch)}}}, s, now=11.0)
    pid, m, prm, sid = s.sent[-1]
    assert m == "Runtime.evaluate" and sid is None and prm["expression"].startswith("window.postMessage(")
    posted = json.loads(prm["expression"][len("window.postMessage("):-len(", '*'); true")])
    assert posted["source"] == "lvqoe-mse" and posted["inst"] == 3.5 and posted["wi"] == 0 and posted["to"] == 1761740000000.25
    assert a.workers[0]["time_origin"] == 1761740000000.25
    assert [r["wi"] for r in posted["recs"]] == [0, 0] and [r["op"] for r in posted["recs"]] == ["app", "tso"]
    a.handle({"id": pid, "result": {"result": {"value": True}}}, s, now=11.01)
    assert (a.batches, a.recs) == (1, 2) and a.kinds["app"][0] == 1 and a.kinds["tso"][1] > 0
    a.take(s)                                                    # an empty batch after the first post: not posted
    tid = s.sent[-1][0]
    a.handle({"id": tid, "result": {"result": {"value": json.dumps({**batch, "recs": []})}}}, s, now=12.0)
    assert s.sent[-1][0] == tid
    a.take(s)                                                    # a post the page refuses: counted, records lost counted
    tid = s.sent[-1][0]
    a.handle({"id": tid, "result": {"result": {"value": json.dumps(batch)}}}, s, now=13.0)
    a.handle({"id": s.sent[-1][0], "error": {"code": -32000, "message": "no context"}}, s, now=13.1)
    assert (a.post_errors, a.post_lost) == (1, 2)
    a.take(s)                                                    # levi 14:59: a malformed take reply is counted, the anchor lives
    a.handle({"id": s.sent[-1][0], "result": {"result": {"value": "{not json"}}}, s, now=13.5)
    assert a.handle_errors == 1 and "JSONDecodeError" in a.handle_last_error
    a.take(s)
    a.handle({"id": s.sent[-1][0], "result": {"result": {"value": json.dumps(batch)}}}, s, now=13.6)
    assert s.sent[-1][1] == "Runtime.evaluate" and s.sent[-1][3] is None              # the next take relays
    a.handle({"id": s.sent[-1][0], "result": {"result": {"value": True}}}, s, now=13.7)
    assert (a.batches, a.recs) == (2, 4)
    a.handle({"method": "Target.detachedFromTarget", "params": {"sessionId": "S1"}}, s, now=14.0)
    n = len(s.sent)
    a.take(s)
    assert len(s.sent) == n and a.workers[0]["detached"] == 14.0                    # a detached worker is never asked
    rec = a.record()
    assert rec["workers"][0]["wrap"] == "ok" and rec["workers"][0]["url_kind"] == "blob" and rec["kinds_on"] == ["app", "aapp", "end"]
    assert rec["kinds"]["app"]["n"] == 3 and rec["recs"] == 4      # kinds: every record posted (the refused post's too) and rec["key_every"] == 30 and rec["eval_timeout_ms"] == 50
    assert rec["handle_errors"] == 1
    assert "posted_inst" not in rec["workers"][0]


def test_install_gating():
    calls = []

    class Fake:
        _ws_url = "ws://x"
        _anchor = None

    for platform, fl in (("youtube", {"enabled": True, "block_s": 0}), ("twitch", None), ("tiktok", {"enabled": False})):
        f = Fake()
        f.platform, f.frame_log = platform, fl
        B.BrowserSession._worker_anchor_install(f)
        calls.append(f._anchor is None and (fl is None or "worker_anchor" not in fl))
    assert all(calls), calls
    assert B.WORKER_ANCHOR_PLATFORMS == ("twitch", "tiktok")


def test_ext_export_record_adds_only_dur_and_timeout():
    """levi 14:51: the record is 971e444e's apart from dur_ms / timeout_s, on ok / failed / no context; a timing fault
    leaves the keys absent and the record still written; exp.py's not_run default (no record at all) is untouched."""
    import session_meta
    got, orig = [], session_meta.write
    session_meta.write = lambda sd, **kw: got.append(kw["ext_export"])
    try:
        for rec in ({"ok": True, "events": 5}, {"ok": False, "error": "RuntimeError('x')"}, {"ok": False, "reason": "no_ext_context"}):
            B._ext_export_record(Path("/nonexistent"), **rec)                         # 971e444e's call form
            B._ext_export_record(Path("/nonexistent"), B.time.time() - 0.25, **rec)
            old, new = got[-2], got[-1]
            assert old == rec and list(new)[:len(rec)] == list(rec) and {k: new[k] for k in rec} == rec
            assert set(new) - set(rec) == {"dur_ms", "timeout_s"} and 240 <= new["dur_ms"] < 5000 and new["timeout_s"] == 20
        B._ext_export_record(Path("/nonexistent"), "not-a-time", ok=True, events=1)  # a timing fault
        assert got[-1] == {"ok": True, "events": 1}
    finally:
        session_meta.write = orig
    assert '"reason": "not_run"' in (HERE / "exp.py").read_text()                 # not_run: exp.py's own default, untouched


def main() -> int:
    for f in (test_worker_wrap_transparent_records_and_keys, test_worker_key_matches_reference_avc_and_hevc,
              test_worker_off_block_keeps_state_and_cap_counts, test_attach_filter_and_resume_always,
              test_relay_detach_and_record, test_install_gating, test_ext_export_record_adds_only_dur_and_timeout):
        f()
    print("test_worker_anchor: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
