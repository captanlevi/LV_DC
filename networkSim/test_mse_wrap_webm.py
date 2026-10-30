#!/usr/bin/env python3
"""t102 rows (3) / (7) + the yt key (levi 22:10; audit 00138): MSE_WRAP_JS's WebM byte-stream state under node, on a
synthetic WebM stream (unknown-size Segment and Clusters, a known-size Cluster, SimpleBlocks either side of 512 B, a
negative relative tc, a laced block, a BlockGroup, a Cluster tc jump):
  - L1: one append, every 2-append cut and 200 seeded 3-20-append cuts give the same keys ([abs, rel, L, fnv, kf]) as
    the stream computed in Python, the same block count, and per append cc = the Cluster tc read before its byte 0 and
    wc = the first Cluster tc read in it with its Cluster's byte offset;
  - remove() between two mid-Cluster appends keeps the carry (audit 1; the mutant that resets on remove fails it);
    abort() / changeType() / a parse fault reset (us abort / ct / err, r2err on the fault) and only an append that starts
    at a Cluster or EBML resyncs;
  - a page that reuses its buffer after appendBuffer never changes a carried key;
  - the key is watcher.media.vcl_keys.key (fixtures/yt_key_twin.json, made from LVQoE master 8c08c9c6), in JS and in the
    Python twin here;
  - off (MSE_WRAP_WEBM / MSE_WRAP_YT_KEY 0): the posts equal 7809b21c's wrap (fixtures/mse_wrap_js_7809b21c.js); key
    only: no Row 1 field; OFF blocks: no key row.

    python3 test_mse_wrap_webm.py        (needs node; exit 0 = pass)
"""
import json
import random
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

MIME = 'video/webm; codecs="vp09.00.40.08"'
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [srcPath, planPath, outPath] = process.argv.slice(2), src = fs.readFileSync(srcPath, "utf8");
const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
const posted = [], timers = [], SENT = { ret: 1 };
class SourceBuffer { constructor() { this.ls = {}; this._tso = 0; this.buffered = { length: 0, end: () => 0 }; }
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); } }
Object.defineProperty(SourceBuffer.prototype, "timestampOffset", { configurable: true, get() { return this._tso; }, set(v) { this._tso = v; } });
let seen = 0;
SourceBuffer.prototype.appendBuffer = function (b) { seen += b.length; return SENT; };
SourceBuffer.prototype.remove = function () {}; SourceBuffer.prototype.changeType = function () {};
SourceBuffer.prototype.abort = function () {};
class MediaSource {} MediaSource.prototype.addSourceBuffer = function () { return new SourceBuffer(); };
const ctx = { MediaSource, SourceBuffer, performance: { now: () => 7 }, Date: { now: () => plan.now_ms }, String, Math,
  Uint8Array, DataView, ArrayBuffer, Error, setInterval: (f) => timers.push(f), postMessage: (m) => posted.push(JSON.parse(JSON.stringify(m))) };
ctx.window = ctx; vm.createContext(ctx); vm.runInContext(src, ctx);
const runs = []; let rets = 0;
for (const ops of plan.runs) {
  const sb = new MediaSource().addSourceBuffer(plan.mime); let last = null;
  for (const op of ops) {
    if (op.app !== undefined) { const a = Uint8Array.from(Buffer.from(op.app, "hex")); rets += sb.appendBuffer(a) === SENT ? 1 : 0; last = a; }
    else if (op.rm) sb.remove(op.rm[0], op.rm[1]);
    else if (op.abort) sb.abort();
    else if (op.ct) sb.changeType(op.ct);
    else if (op.scribble && last) last.fill(0);                       // the page reuses its buffer after the append
  }
  const n0 = posted.length; for (const f of timers) f();
  runs.push(posted.slice(n0).flatMap((p) => p.recs).filter((r) => r.op === "app"));
}
fs.writeFileSync(outPath, JSON.stringify({ runs, rets, seen, posted_keys: posted.map((p) => Object.keys(p)), ms: posted.map((p) => p.ms) }));
"""


def run(src: str, runs: list, now_ms: int = 1_000_000, mime: str = MIME) -> dict:
    node = shutil.which("node")
    assert node, "node not found: the WebM wrap test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, js, plan, out = (Path(t) / x for x in ("h.js", "w.js", "plan.json", "out.json"))
        h.write_text(HARNESS)
        js.write_text(src)
        plan.write_text(json.dumps({"now_ms": now_ms, "mime": mime, "runs": runs}))
        subprocess.run([node, str(h), str(js), str(plan), str(out)], check=True)
        return json.loads(out.read_text())


def wrap(**k) -> str:
    k = {"r2": True, "webm": True, "ykey": True, "key_every": 1, **k}
    return B.mse_wrap_js(k.pop("block_s", 0), max_recs=k.pop("max_recs", 2000), **k)


# ---- the key, Python twin of LVQoE watcher.media.vcl_keys.key ----
def fnv1a32(b: bytes) -> int:
    h = 0x811C9DC5
    for x in b:
        h = ((h ^ x) * 0x01000193) & 0xFFFFFFFF
    return h


def key(p: bytes) -> list:
    return [len(p), fnv1a32(p[:256] + p[-256:] if len(p) > 512 else p)]


# ---- a synthetic WebM stream with its truth ----
UNKNOWN = b"\x01\xff\xff\xff\xff\xff\xff\xff"


def size(n: int) -> bytes:
    for w in range(1, 9):
        if n < (1 << (7 * w)) - 1:
            return ((1 << (7 * w)) | n).to_bytes(w, "big") if w > 1 else bytes([0x80 | n])
    raise ValueError(n)


def el(id_: bytes, payload: bytes = b"", unknown: bool = False) -> bytes:
    return id_ + (UNKNOWN if unknown else size(len(payload))) + payload


def uint(v: int, w: int) -> bytes:
    return v.to_bytes(w, "big")


def stream(rnd: random.Random):
    """(bytes, blocks [(abs, rel, L, key or None (laced), kf or None), ...], timecodes [(end pos, value, cluster id pos)])."""
    out, blocks, tcs = bytearray(), [], []

    def put(b: bytes):
        out.extend(b)

    def cluster(tc: int, items: list, unknown: bool):
        body, marks = bytearray(), []
        tce = el(b"\xe7", uint(tc, 4 if tc > 0xFFFFFF else 3))
        body += tce
        marks.append(("tc", len(body), tc))
        for kind, rel, L, flags in items:
            p = bytes(rnd.randrange(256) for _ in range(L))
            hdr = b"\x81" + (rel & 0xFFFF).to_bytes(2, "big") + bytes([flags])
            if kind == "sb":
                body += el(b"\xa3", hdr + p)
                blocks.append((tc + rel, rel, L, None if flags & 0x06 else key(p), None if flags & 0x06 else flags >> 7))
            else:                                               # BlockGroup { Block, ReferenceBlock }
                body += el(b"\xa0", el(b"\xa1", hdr + p) + el(b"\xfb", b"\x01"))
                blocks.append((tc + rel, rel, L, key(p), None))
        cid = len(out)
        head = b"\x1f\x43\xb6\x75" + (UNKNOWN if unknown else size(len(body)))
        put(head + bytes(body))
        for m in marks:
            tcs.append((cid + len(head) + m[1], m[2], cid))

    put(el(b"\x1a\x45\xdf\xa3", el(b"\x42\x86", b"\x01") + el(b"\x42\x82", b"webm")))
    put(b"\x18\x53\x80\x67" + UNKNOWN)
    put(el(b"\x15\x49\xa9\x66", el(b"\x2a\xd7\xb1", uint(1_000_000, 3))))
    put(el(b"\x16\x54\xae\x6b", el(b"\xae", el(b"\xd7", b"\x01"))))
    put(el(b"\xec", bytes(7)))                                   # Void
    cluster(1000, [("sb", 0, 100, 0x80), ("sb", 33, 700, 0x00), ("sb", 50, 40, 0x02), ("bg", 66, 600, 0)], True)
    cluster(2000, [("sb", 0, 513, 0x80), ("sb", -5, 512, 0x00), ("sb", 33, 256, 0x00)], False)
    cluster(10_000_000, [("sb", 0, 300, 0x80), ("sb", 33, 1, 0x00)], True)           # a tc jump (a rebase's shape)
    return bytes(out), blocks, tcs


def keys_of(apps: list) -> list:
    return [tuple(k) for a in apps for k in a.get("key", [])]


def want_keys(blocks: list) -> list:
    return [(a, r, L, k[1], kf) for a, r, L, k, kf in blocks if k is not None]


def cc_at(tcs: list, i: int):
    v = None
    for end, tc, _ in tcs:
        if end <= i:
            v = tc
    return v


def wc_after(tcs: list, i: int, j: int):
    for end, tc, cid in tcs:
        if i < end <= j:
            return [tc, cid - i]
    return None


def test_key_twin():
    cases = json.loads((HERE / "fixtures" / "yt_key_twin.json").read_text())["cases"]
    for c in cases:
        assert key(bytes.fromhex(c["hex"])) == c["key"], c["key"]
    # JS: one Cluster per case, every block keyed; its key equals the LVQoE key
    body = b"".join(el(b"\xa3", b"\x81\x00\x00\x80" + bytes.fromhex(c["hex"])) for c in cases)
    s = el(b"\x1a\x45\xdf\xa3", b"") + b"\x18\x53\x80\x67" + UNKNOWN + b"\x1f\x43\xb6\x75" + UNKNOWN + el(b"\xe7", b"\x01") + body
    got = keys_of(run(wrap(), [[{"app": s.hex()}]])["runs"][0])
    assert [[k[2], k[3]] for k in got] == [c["key"] for c in cases], got


def test_l1_every_cut_and_random_cuts():
    rnd = random.Random(7)
    s, blocks, tcs = stream(rnd)
    want = want_keys(blocks)
    one = run(wrap(), [[{"app": s.hex()}]])["runs"][0]
    assert keys_of(one) == want and one[0]["nb"] == len(blocks) and one[0]["lace"] == 1, (keys_of(one), want)
    assert one[0]["cc"] is None and one[0]["wc"] == [1000, tcs[0][2]] and "us" not in one[0]
    cuts = list(range(4, len(s)))                # a first append under 4 bytes cannot show the EBML ID: unsynced by rule
    runs = [[{"app": s[:i].hex()}, {"app": s[i:].hex()}] for i in cuts]
    res = run(wrap(), runs)
    assert res["rets"] == 2 * len(cuts)
    for i, apps in zip(cuts, res["runs"]):
        assert keys_of(apps) == want, i
        assert sum(a.get("nb", 0) for a in apps) == len(blocks), i
        assert apps[1]["cc"] == cc_at(tcs, i), (i, apps[1]["cc"])
        assert apps[1].get("wc") == wc_after(tcs, i, len(s)), (i, apps[1].get("wc"))
        assert not any("us" in a or "r2err" in a for a in apps), i
    rr = random.Random(11)
    runs, plans = [], []
    for _ in range(200):
        k = rr.randint(3, 20)
        pos = sorted(rr.sample(range(4, len(s)), k - 1))
        plans.append(pos)
        runs.append([{"app": s[a:b].hex()} for a, b in zip([0] + pos, pos + [len(s)])])
    for pos, apps in zip(plans, run(wrap(), runs)["runs"]):
        assert keys_of(apps) == want and sum(a.get("nb", 0) for a in apps) == len(blocks), pos
        assert [a["cc"] for a in apps] == [cc_at(tcs, p) for p in [0] + pos], pos


def _mid(s: bytes, tcs: list) -> int:
    return tcs[0][0] + 60                    # inside Cluster 1, past its Timecode, inside a block


def test_remove_keeps_the_carry_and_its_mutant_fails():
    s, blocks, tcs = stream(random.Random(7))
    i = _mid(s, tcs)
    plan = [[{"app": s[:i].hex()}, {"rm": [0, 1]}, {"app": s[i:].hex()}]]
    apps = run(wrap(), plan)["runs"][0]
    assert apps[1]["cc"] == 1000 and keys_of(apps) == want_keys(blocks) and "us" not in apps[1]
    mutant = wrap().replace('wrap(p, "remove", (sb, a) => push({ op: "rm", sb: id(sb), s: a[0], e: a[1] }));',
                            'wrap(p, "remove", (sb, a) => { push({ op: "rm", sb: id(sb), s: a[0], e: a[1] }); reset(W(sb), "rm"); });')
    assert mutant != wrap()
    m = run(mutant, plan)["runs"][0]
    assert m[1].get("us") == "rm" and keys_of(m) != want_keys(blocks)     # the test sees a reset on remove


def test_abort_changetype_fault_reset_and_resync_only_at_a_cluster():
    s, blocks, tcs = stream(random.Random(7))
    i, c2 = _mid(s, tcs), tcs[1][2]                         # mid Cluster 1; Cluster 2's ID
    for op, why in (({"abort": 1}, "abort"), ({"ct": MIME}, "ct")):
        apps = run(wrap(), [[{"app": s[:i].hex()}, op, {"app": s[i:c2].hex()}, {"app": s[c2:].hex()}]])["runs"][0]
        assert apps[1]["us"] == why and "key" not in apps[1] and "cc" not in apps[1], apps[1]
        assert "us" not in apps[2] and apps[2]["cc"] is None and apps[2]["wc"] == [2000, 0]
        assert keys_of(apps[2:]) == [k for k in want_keys(blocks) if k[0] >= 2000 - 5]
    bad = s[:tcs[0][0]] + b"\xa3" + UNKNOWN + b"\x81"         # at an element boundary, a SimpleBlock of unknown size
    apps = run(wrap(), [[{"app": bad.hex()}, {"app": s[i:c2].hex()}, {"app": s[c2:].hex()}]])["runs"][0]
    assert apps[0]["r2err"] == 1 and apps[1]["us"] == "err" and "us" not in apps[2] and apps[2]["wc"] == [2000, 0]
    start = run(wrap(), [[{"app": s[i:].hex()}]])["runs"][0]     # the first append is mid-stream: start, unparsed
    assert start[0]["us"] == "start" and "key" not in start[0]


def test_a_reused_page_buffer_never_changes_a_carried_key():
    s, blocks, tcs = stream(random.Random(7))
    for i in range(tcs[0][0] + 1, tcs[0][0] + 900, 7):
        apps = run(wrap(), [[{"app": s[:i].hex()}, {"scribble": 1}, {"app": s[i:].hex()}]])["runs"][0]
        assert keys_of(apps) == want_keys(blocks), i


def test_off_is_7809b21c_and_key_only_and_off_blocks():
    import test_mse_wrap as T
    old = (HERE / "fixtures" / "mse_wrap_js_7809b21c.js").read_text() % {"block_s": 0, "max": 2000, "r2": 1}
    off = B.mse_wrap_js(0, max_recs=2000, r2=True, webm=False, ykey=False)
    s, blocks, tcs = stream(random.Random(7))
    third = len(s) // 3
    plans = [(m, [st for st in steps]) for m, steps in T.r2_plans()]
    for mime, steps in plans:
        assert json.dumps(T.run_plan(off, steps, mime), sort_keys=True) == json.dumps(T.run_plan(old, steps, mime), sort_keys=True)
    cut = [[{"app": s[:third].hex()}, {"app": s[third:2 * third].hex()}, {"app": s[2 * third:].hex()}]]
    assert json.dumps(run(off, cut), sort_keys=True) == json.dumps(run(old, cut), sort_keys=True)
    ko = run(B.mse_wrap_js(0, r2=True, webm=False, ykey=True, key_every=1), cut)
    assert keys_of(ko["runs"][0]) == want_keys(blocks)
    assert not any(f in a for a in ko["runs"][0] for f in ("cc", "wc", "nb", "b0", "lace", "us"))
    assert all("ms" in k for k in ko["posted_keys"])
    on = run(wrap(block_s=60), cut, now_ms=60_000 + 5_000)            # ON block: keys
    offb = run(wrap(block_s=60), cut, now_ms=120_000 + 5_000)         # OFF block: no record at all
    assert keys_of(on["runs"][0]) == want_keys(blocks) and offb["runs"][0] == []
    n30 = run(wrap(key_every=30), [[{"app": s.hex()}]])["runs"][0]     # the default count: the first block after the
    assert [k[0] for k in keys_of(n30)] == [1000, 10_000_000]          # sync, then the tc jump; nothing by count here
    assert "%(webm)d" in B.MSE_WRAP_YT_JS and "WEBM = " not in (HERE / "fixtures" / "mse_wrap_js_7809b21c.js").read_text()
    # fast gate G0: with both off, the rendered script is 7809b21c's byte for byte, whatever block_s / max / r2
    fx = (HERE / "fixtures" / "mse_wrap_js_7809b21c.js").read_text()
    for bs, mx, r2 in ((0, 2000, True), (60, 2000, True), (60, 7, False)):
        assert B.mse_wrap_js(bs, max_recs=mx, r2=r2, webm=False, ykey=False) == fx % {"block_s": bs, "max": mx, "r2": int(r2)}


def test_mutants_fail():
    """fast gate G1: a dropped carry and a key over the block header (not the payload only) each fail the L1 / twin check."""
    s, blocks, tcs = stream(random.Random(7))
    want = want_keys(blocks)
    src = wrap()
    drop = src.replace("s.q = s.q.map((c) => [c[0].slice(c[1]), 0]);", "s.q = []; s.qn = 0;")
    hdr = src.replace("const tk = yield* vr(s, false), hb = yield* need(s, 3), L = n - tk[1] - 3;",
                      "const tk = yield* vr(s, false), hb = yield* need(s, 3), L = n - tk[1] - 3; s.hh = fnv(0x811c9dc5, hb);")
    hdr = hdr.replace("if (L <= 512) h = fnv(0x811c9dc5, yield* need(s, L));", "if (L <= 512) h = fnv(s.hh, yield* need(s, L));")
    assert drop != src and hdr != src
    cut = [[{"app": s[:i].hex()}, {"app": s[i:].hex()}] for i in range(tcs[0][0] + 1, tcs[0][0] + 40)]
    assert any(keys_of(apps) != want for apps in run(drop, cut)["runs"])                  # the carry dropped: seen
    assert keys_of(run(hdr, [[{"app": s.hex()}]])["runs"][0]) != want                      # header in the key: seen


def main() -> int:
    test_key_twin()
    test_l1_every_cut_and_random_cuts()
    test_remove_keeps_the_carry_and_its_mutant_fails()
    test_abort_changetype_fault_reset_and_resync_only_at_a_cluster()
    test_a_reused_page_buffer_never_changes_a_carried_key()
    test_off_is_7809b21c_and_key_only_and_off_blocks()
    test_mutants_fail()
    print("test_mse_wrap_webm: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
