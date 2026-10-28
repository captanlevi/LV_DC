#!/usr/bin/env python3
"""v3 A1 (t67): content.js's requestVideoFrameCallback aggregates (side stream "frames"), under node with a stubbed
element whose requestVideoFrameCallback stores the callback and the harness presents frames:
  - aggregates only: no row per frame; a dims row at the first presented frame and at each dims change, with that
    frame's presentationTime / expectedDisplayTime / mediaTime / presentedFrames and the previous dims;
  - a first_frame row at the first frame after an attach and after a src_key change (tt's MediaStream anchor);
  - one tick row per 1 s res_meta tick, joined to its event_id, with presented / decoded / dropped side by side and the
    callbacks since the last tick, also while nothing is presented (cb 0, the last frame's values kept);
  - lvqoe_config.rvfc === false registers no callback and the tick rows say rvfc false (the gate's off arm);
  - t91: lvqoe_config.frame_log adds the tick row's field fl, one columnar integer batch per tick of every presented
    frame since the last tick with every metadata field the browser gave (absent stays absent), decoded exactly by
    frame_log.decode (content.js's FL_CODE is the decoder's); seq per tick, keys when they change, values not whole us
    counted, the cap's overflow counted; with the field stripped the frames stream is the one without it; alternating
    blocks log only in the ON blocks; no config, no field.

    python3 test_rvfc_frames.py        (needs node; exit 0 = pass)
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import frame_log as FL  # noqa: E402

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const mode = process.argv[4];
const off = mode.startsWith("off");
const asyncCfg = mode.endsWith("async");
const pendingCfg = [];
const stored = {};
let failNextSide = mode === "on" || mode === "fl1";   // the first side write fails once (re-queued, counted)
const cfgFor = { off: { lvqoe_config: { instruments: false } },     // t91: frame_log on, or in 1 s OFF / ON blocks from 0
                 fl1: { lvqoe_config: { frame_log: { block_s: 0, t0: 5e11 } } },
                 flalt: { lvqoe_config: { frame_log: { block_s: 1, t0: 5e11 } } },
                 flcap: { lvqoe_config: { frame_log: { block_s: 0, t0: 5e11 } } },
                 flfull: { lvqoe_config: { frame_log: { block_s: 0, t0: 5e11 } } },
                 flthrow: { lvqoe_config: { frame_log: { block_s: 0, t0: 5e11 } } },
                 flbare: { lvqoe_config: { frame_log: { block_s: 0, t0: 5e11 } } } };
const bare = mode === "flbare";             // t93 absent paths: a browser without longtask / connection, no page event
const timers = [], side = [], sent = [];
let now = 1000;
class MediaStream {}
const cbs = [];
let decoded = 0;
const v = { paused: false, ended: false, readyState: 4, networkState: 2, currentTime: 0, videoWidth: 1280,
  videoHeight: 720, clientWidth: 853, clientHeight: 480, playbackRate: 1, duration: Infinity, isConnected: true,
  currentSrc: "", srcObject: new MediaStream(), buffered: { length: 0 },
  getVideoPlaybackQuality: () => ({ droppedVideoFrames: 2, totalVideoFrames: decoded,
                                    ...(mode === "flfull" ? { corruptedVideoFrames: 0, creationTime: now } : {}) }),
  ...(mode === "flfull" ? { webkitDecodedFrameCount: 5, webkitVideoDecodedByteCount: 1234 } : {}),
  addEventListener() {}, requestVideoFrameCallback(f) { cbs.push(f); return cbs.length; } };
const ctx = {
  console: { log() {} }, performance: { timeOrigin: 5e11, now: () => now },
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {}, Date: { now: () => 5e11 + now },
  MutationObserver: class { observe() {} disconnect() {} },
  XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
  location: { href: "https://www.tiktok.com/@r/live", hostname: "www.tiktok.com" },
  chrome: { storage: { local: { get: (d, cb) => { const r = () => cb(cfgFor[mode.replace("_async", "")] || d);
                                                    if (asyncCfg) pendingCfg.push(r); else r(); },
                                set: (o) => Object.assign(stored, o) } },
            runtime: { lastError: null, sendMessage: (m, cb) => {
              if (m.kind === "side-batch" && failNextSide) { failNextSide = false; cb && cb({ ok: false, err: "quota" }); return; }
              if (m.kind === "side-batch") side.push(m); if (m.kind === "video-events-batch") sent.push(...m.payload);
              cb && cb({ ok: true }); } } },
  addEventListener(t, f) { (winL[t] = winL[t] || []).push(f); },
  getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  navigator: bare ? {} : { connection: { downlink: 9.5, rtt: 50, effectiveType: "4g", saveData: false } },
  PerformanceObserver: bare ? undefined
    : class { constructor(f) { ltCb.push(f); } observe() {} static get supportedEntryTypes() { return ["longtask"]; } },
};
const winL = {}, docL = {}, ltCb = [];
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: {} }, body: {}, visibilityState: "visible", hidden: false,
  addEventListener(t, f) { (docL[t] = docL[t] || []).push(f); },
  querySelectorAll: (q) => (q === "video" ? [v] : []), querySelector: () => null, evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const registeredBeforeCfg = cbs.length;    // async config: nothing may register before the config is read
for (const r of pendingCfg.splice(0)) r();
if (mode === "exact") {                     // levi 22:44 (2): 100 batches of 1000 frames through the encoder itself
  const batches = [], want = [];
  const R = (v, sc) => Math.round(v * sc) / sc;
  for (let b = 0; b < 100; b++) {
    for (let i = 0; i < 1000; i++) {
      const k = b * 1000 + i;
      const md = { presentationTime: 1000 + k * 1000 / 60, expectedDisplayTime: 1000 + (k + 1) * 1000 / 60,
                   mediaTime: 7.25 + k / 30, presentedFrames: k + 1, width: 1280, height: 720 };
      const cbNow = md.presentationTime + 0.3;
      ctx.__push = [k % 3 ? 1 : 2, cbNow, md];
      vm.runInContext("frameLogPush(__push[0], __push[1], __push[2])", ctx);
      want.push({ el: k % 3 ? 1 : 2, pt: R(md.presentationTime, 1e3), edt: R(md.expectedDisplayTime, 1e3), mt: R(md.mediaTime, 1e6),
                  pf: k + 1, w: 1280, h: 720, now: R(cbNow, 1e3) });
    }
    batches.push(JSON.parse(vm.runInContext("JSON.stringify({ n: frameN, ...frameLogEncode(frameN) })", ctx)));
    vm.runInContext("frameN = 0", ctx);
  }
  fs.writeFileSync(process.argv[3], JSON.stringify({ batches, want }));
  process.exit(0);
}
let pf = 0, mt = 0;
const thrown = [];
const presented = [];
const present = (n, w, h) => { for (let i = 0; i < n; i++) {
  const f = cbs.shift(); if (!f) return; now += 33; pf += 1; mt += 1 / 30; decoded += 1;
  const md = { presentationTime: now - 1, expectedDisplayTime: now + 16, mediaTime: mt, presentedFrames: pf, width: w, height: h };
  if (mode === "flfull" && pf % 2) Object.assign(md, { processingDuration: (pf * 7) / 1e6, captureTime: now - 40,
                                                       receiveTime: now - 20.5, rtpTimestamp: pf * 3000 });   // t91: every field
  presented.push(mode === "flthrow" ? { ...md } : md);   // the record before any faulting getter
  if (mode === "flthrow" && pf === 10)      // levi 23:51 (1): a fault inside frameLogPush (contained, counted)
    Object.defineProperty(md, "processingDuration", { get() { throw new Error("pd fault"); } });
  if (mode === "flthrow" && pf === 20)      // and a throw in the callback body outside it (reaches the caller)
    Object.defineProperty(md, "width", { get() { throw new Error("body fault"); } });
  try { f(now, md); } catch (e) { thrown.push(String(e)); } } };
const tick = () => { now += 1; for (const t of timers) t(); };
present(30, 720, 1280);
if (mode.startsWith("fl") && !bare) {      // t93: a long task and two page events inside the first (ON) tick
  for (const f of ltCb) f({ getEntries: () => [{ duration: 72.5 }, { duration: 51 }] });
  ctx.document.hidden = true; ctx.document.visibilityState = "hidden"; for (const f of docL.visibilitychange || []) f();
  for (const f of winL.blur || []) f();
  ctx.document.hidden = false; ctx.document.visibilityState = "visible";
  for (const f of winL.message || []) f({ source: vm.runInContext("window", ctx), data: { source: "lvqoe-mse", over: 0,
    recs: [{ op: "add", t: 5, sb: 1, mime: 'video/mp4; codecs="avc1.4d401f"' }, { op: "app", t: 6, sb: 1, bytes: 1000 }] } });
}
tick();                                     // 30 frames, one tick
present(10, 720, 1280); present(5, 540, 960); tick();   // a rendition switch inside the second
tick();                                     // nothing presented in this second
v.srcObject = new MediaStream(); mt = 0;   // a new stream (tt re-entry): mediaTime restarts
present(3, 540, 960); tick();
if (mode === "flcap") { present(1300, 540, 960); tick(); }   // over FRAME_LOG_MAX in one tick
vm.runInContext("flushSide()", ctx);        // the last tick's rows
const flCode = vm.runInContext("JSON.stringify(FL_CODE)", ctx);
fs.writeFileSync(process.argv[3], JSON.stringify({ side, sent, pending: cbs.length, registeredBeforeCfg, stored, presented, thrown,
                                                   flCode: JSON.parse(flCode) }));
"""


def run(mode: str) -> dict:
    node = shutil.which("node")
    assert node, "node not found: the content.js rVFC test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, out = Path(t) / "h.js", Path(t) / "out.json"
        h.write_text(HARNESS)
        subprocess.run([node, str(h), str(HERE.parent / "labeling" / "content.js"), str(out), mode], check=True)
        return json.loads(out.read_text())


def test_rvfc_frames():
    r = run("on")
    fr = [x for m in r["side"] if m["stream"] == "frames" for x in m["payload"]]
    kinds = [x["k"] for x in fr]
    assert len(fr) < 48 and kinds.count("tick") == 4, kinds                    # aggregates, never one row per frame
    dims = [x for x in fr if x["k"] == "dims"]
    assert [(d["w"], d["h"], d["prev_w"], d["prev_h"], d["pf"]) for d in dims] == \
        [(720, 1280, None, None, 1), (540, 960, 720, 1280, 41)], dims
    d = dims[1]
    assert d["ts"] == 5e11 + d["pt"] + 1 and d["edt"] == d["pt"] + 17 and abs(d["mt"] - 41 / 30) < 1e-9
    ff = [x for x in fr if x["k"] == "first_frame"]
    assert [(x["src"], x["prev_src"], x["pf"]) for x in ff] == [("MediaStream#1", None, 1), ("MediaStream#2", "MediaStream#1", 46)], ff
    assert abs(ff[1]["mt"] - 1 / 30) < 1e-9                                     # the new stream's own mediaTime
    ticks = [x for x in fr if x["k"] == "tick"]
    res_ids = [e["event_id"] for e in r["sent"] if e["type"] == "res_meta"]
    assert [t["tick"] for t in ticks] == res_ids
    assert [(t["cb"], t["pf"], t["decoded"], t["dropped"], t["rvfc"]) for t in ticks] == \
        [(30, 30, 30, 2, True), (15, 45, 45, 2, True), (0, 45, 45, 2, True), (3, 48, 48, 2, True)], ticks
    assert r["pending"] == 1                                                    # always one callback registered
    # brad 18:17: the first side write failed once: re-queued (no row lost, the 4 ticks above), counted, nothing dropped
    assert sum(ticks[-1]["side_failed"].values()) == 1 and ticks[-1]["side_dropped"] == {"player_events": 0, "frames": 0, "ad_edges": 0}
    assert all(t["off_arm_cb"] == 0 for t in ticks)
    sc = r["stored"]["side_counters"]                                          # levi 18:26: the final counters persisted
    assert (sc["side_dropped"], sc["side_failed"], sc["off_arm_cb"]) == (ticks[-1]["side_dropped"], ticks[-1]["side_failed"], 0)
    for mode in ("off", "off_async"):                                           # the off arm, config read late or early
        off = run(mode)
        fo = [x for m in off["side"] if m["stream"] == "frames" for x in m["payload"]]
        assert off["pending"] == 0 and off["registeredBeforeCfg"] == 0, mode
        assert {x["k"] for x in fo} == {"tick"} and all(x["rvfc"] is False and x["cb"] is None and x["off_arm_cb"] == 0 for x in fo)
        # levi 18:33: the off arm starts no instrument at all: no player events, no ad edges, no box, none counted
        assert not [m for m in off["side"] if m["stream"] in ("player_events", "ad_edges") and m["payload"]], mode
        assert all(x["instruments"] is False and x["box"] is None and x["ad_checks"] == 0 and x["src3_polls"] == 0 for x in fo)
    late = run("on_async")                                                      # on, config read after the attach
    fl = [x for m in late["side"] if m["stream"] == "frames" for x in m["payload"]]
    assert late["registeredBeforeCfg"] == 0 and late["pending"] == 1 and [x["k"] for x in fl][:2] == ["first_frame", "dims"]


def _frames(r: dict) -> list[dict]:
    return [x for m in r["side"] if m["stream"] == "frames" for x in m["payload"]]


def _exact(v: float, scale: float) -> bool:
    return round(v * scale) / scale == v


def test_frame_log():
    r1 = run("fl1")
    base, fl1 = _frames(run("on")), _frames(r1)
    assert not any("fl" in x for x in base)                                    # no config: the tick rows as before
    # reader neutrality at the source: with fl stripped the stream is the one without the per-frame log
    assert [{k: v for k, v in x.items() if k != "fl"} for x in fl1 if x["k"] != "mse"] == base
    fl = [x["fl"] for x in fl1 if x["k"] == "tick"]
    assert [f["seq"] for f in fl] == [0, 1, 2, 3] and all(f["on"] and f["over"] == 0 for f in fl)
    assert {f["cs"] for f in fl} == {e["cs"] for e in r1["sent"]} and len({f["cs"] for f in fl}) == 1   # the instance's CS_ID
    assert [f["n"] for f in fl] == [30, 15, 0, 3]                              # = the tick's delta pf
    # the collector's decoder is content.js's format: the same columns, scales and codes in the same order
    assert {k: list(v) for k, v in r1["flCode"].items()} == {k: list(v) for k, v in FL.FL_CODE.items()}
    rows = [row for f in fl for row in FL.decode(f)]
    assert [row["pf"] for row in rows] == list(range(1, 49))                  # every presented frame, pf by pf
    assert [(row["w"], row["h"]) for row in rows] == [(720, 1280)] * 40 + [(540, 960)] * 8
    assert all(set(row) == {"el", "pt", "edt", "mt", "pf", "w", "h", "now"} for row in rows)   # absent fields stay absent
    for row, md in zip(rows, r1["presented"]):                                # exact to the us, both clocks and mt
        assert row["el"] == 1 and row["pt"] == md["presentationTime"] and row["edt"] == md["expectedDisplayTime"]
        assert row["now"] == row["pt"] + 1                                      # brad W1: the callback's now (harness: pt + 1)
        assert abs(row["mt"] - md["mediaTime"]) <= 0.5e-6
    assert sum(f["inexact"] for f in fl) == sum(not _exact(md["mediaTime"], 1e6) for md in r1["presented"])
    assert fl[0]["keys"] == "expectedDisplayTime,height,mediaTime,presentationTime,presentedFrames,width"
    assert all("keys" not in f for f in fl[1:])                               # sent again only when it changes
    assert fl[0]["c"]["pf"] == [1] * 30 and set(fl[0]["c"]["edt"]) == {17000} and fl[2]["c"] == {}   # ints, delta / pt offset
    full = run("flfull")                                                        # every metadata field, some frames only
    ff = [x["fl"] for x in _frames(full) if x["k"] == "tick"]
    rows = [row for f in ff for row in FL.decode(f)]
    for row, md in zip(rows, full["presented"]):
        for col, (field, _, _) in FL.FL_CODE.items():
            if field in ("mediaTime", None):          # mt: the us rounding above; el / now: not metadata
                continue
            assert row.get(col) == md.get(field), (col, row, md)              # present or absent, as the browser gave it
    assert sum(1 for row in rows if "pd" in row) == 24 and all(row["rtp"] == row["pf"] * 3000 for row in rows if "rtp" in row)
    assert ff[0]["c"]["pd"][:2] == [7, None] and ff[0]["c"]["ct"][0] == -39000 and ff[0]["c"]["rt"][0] == -19500
    # t93 page rows (levi 00124), ON ticks: to / wall / hid, q / wk only with what the browser has, long tasks, connection
    f0 = fl[0]
    assert f0["to"] == 5e11 and f0["wall"] == 5e11 + 1991 and f0["hid"] is False
    assert f0["q"] == {} and f0["wk"] == {}                                    # absent stays absent, never 0
    assert f0["lt"] == {"n": 2, "max_ms": 72.5} and fl[1]["lt"] == {"n": 0, "max_ms": 0}
    assert f0["conn"] == {"dl": 9.5, "rtt": 50, "et": "4g", "sd": False} and "err" not in f0
    pe = [x for m in r1["side"] if m["stream"] == "player_events" for x in m["payload"] if x["ev"] == "lvqoe_page"]
    assert [(x["kind"], x["hid"], x["vis"]) for x in pe] == [("visibilitychange", True, "hidden"), ("blur", True, "hidden")]
    mse = [x for x in fl1 if x["k"] == "mse"]                                  # the page-world wrap's batch, forwarded
    assert [(x["op"], x["sb"]) for x in mse] == [("add", 1), ("app", 1)] and mse[1]["bytes"] == 1000
    assert all(t["fl"]["mse_over"] == 0 for t in fl1 if t["k"] == "tick")
    assert f0["mse_inst"] == {"inst": None, "first": None}                    # the harness's batch carries neither
    full_f0 = [x["fl"] for x in _frames(run("flfull")) if x["k"] == "tick"][0]
    assert full_f0["q"] == {"corrupted": 0, "creation": 1991} and full_f0["wk"] == {"dfc": 5, "vb": 1234}
    alt = [x["fl"] for x in _frames(run("flalt")) if x["k"] == "tick"]         # block 1 ON (frames to 2.49 s), block 2 OFF
    assert [(f["on"], f["n"]) for f in alt] == [(True, 30), (True, 15), (False, 0), (False, 0)], alt
    assert all(k not in f for f in alt[2:] for k in ("to", "q", "wk", "lt", "conn"))   # OFF ticks: no new reading
    cap = [x["fl"] for x in _frames(run("flcap")) if x["k"] == "tick"]
    assert cap[-1]["n"] == 1200 and cap[-1]["over"] == 100 and FL.decode(cap[-1])[-1]["pf"] == 48 + 1200
    off = _frames(run("off"))                                                   # instruments off: no field
    assert not any("fl" in x for x in off)


def test_frame_log_absent_paths():
    """t93 rows' absent paths (levi 23:22 (2)): no longtask support -> lt null; no navigator.connection -> conn {}; no
    getVideoPlaybackQuality extras / webkit counters -> q / wk {}; no page event -> no lvqoe_page; no MSE message (tw,
    tt, an unwrapped yt document) -> no mse_inst / mse_over; no metadata field -> its column absent. Never 0, never err."""
    r = run("flbare")
    fl = [x["fl"] for x in _frames(r) if x["k"] == "tick"]
    assert [f["n"] for f in fl] == [30, 15, 0, 3]
    for f in fl:
        assert f["lt"] is None and f["conn"] == {} and f["q"] == {} and f["wk"] == {}, f
        assert not any(k in f for k in ("mse_inst", "mse_over", "err", "push_err")), f
        assert set(f["c"]) <= {"el", "pt", "edt", "mt", "pf", "w", "h", "now"}
    assert not [x for m in r["side"] if m["stream"] == "player_events" for x in m["payload"] if x["ev"] == "lvqoe_page"]


def test_frame_log_cb_throw():
    """levi 23:51 (1): a throw in the rVFC callback never ends the chain (the re-request is in finally); a fault inside
    frameLogPush is contained: that frame's row is not kept, fl.push_err counts it, fl.err names it."""
    r = run("flthrow")
    assert r["thrown"] == ["Error: body fault"], r["thrown"]                   # only the body throw reaches the caller
    assert r["pending"] == 1                                                    # the chain is still armed at the end
    fl = [x["fl"] for x in _frames(r) if x["k"] == "tick"]
    assert fl[0]["n"] == 28 and fl[0]["push_err"] == 1 and fl[0]["err"] == "push: Error: pd fault"
    assert [f["n"] for f in fl[1:]] == [15, 0, 3] and not any("push_err" in f for f in fl[1:])
    assert [row["pf"] for row in FL.decode(fl[0])] == [p for p in range(1, 31) if p not in (10, 20)]


def test_frame_log_exact_over_length():
    """levi 22:44 (2): deltas are between the rounded integers, so decode gives back every rounded absolute with no
    drift: 100 000 frames (mt at 1/30 s, pt at 60 Hz, edt / now offsets, two elements), decoded = rounded exactly."""
    r = run("exact")
    rows = [row for b in r["batches"] for row in FL.decode({"n": b["n"], "c": b["cols"]})]
    assert len(rows) == len(r["want"]) == 100_000
    assert rows == r["want"], next((i, a, b) for i, (a, b) in enumerate(zip(rows, r["want"])) if a != b)
    assert sum(b["inexact"] for b in r["batches"]) > 0                         # the 1/30 s and 60 Hz values were rounded


def main() -> int:
    test_rvfc_frames()
    test_frame_log_absent_paths()
    test_frame_log_cb_throw()
    test_frame_log_exact_over_length()
    test_frame_log()
    print("test_rvfc_frames: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
