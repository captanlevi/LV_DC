#!/usr/bin/env python3
"""Boundary after window 1 (codex item 1, levi 00106): the extension's label-event path loses nothing silently.
Drives the real labeling/content.js and labeling/background.js under node with stubbed chrome APIs:
  - background.js: two frames' batches interleaved on an async storage are both stored (one serialized append chain;
    unqueued get / get / set / set lost one), frameId / documentId on every row, a resent batch is not doubled, the ack
    carries the highest stored event_id; two concurrent player_stats batches are both stored;
  - content.js: an ack that lands after the overflow shifted the in-flight batch drops only the acked ids (never by
    count); an unacked event the overflow shifts out is counted, and the next batch carries the cumulative
    {n, first_id, last_id}, which background.js stamps on its rows (dropped_n / dropped_first_id / dropped_last_id).
And browser.py's export: player_stats.jsonl and labels.csv are on disk before chrome.storage is cleared; a failed
labels.csv write leaves storage uncleared.

    python3 -m pytest test_ext_event_path.py        (the JS part needs node; skipped without it)
"""
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402

LAB = HERE.parent / "labeling"

BG_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const store = {};
const q = [];                                   // async storage: every get / set callback runs later, FIFO
const clone = (x) => JSON.parse(JSON.stringify(x));
let listener = null;
const bg = { console: { log() {} }, Date: { now: () => 42 }, String, Math,
  chrome: { runtime: { lastError: null, onMessage: { addListener: (f) => { listener = f; } } },
            storage: { local: {
              get: (d, cb) => q.push(() => cb({ ...clone(d), ...clone(store) })),
              set: (o, cb) => q.push(() => { Object.assign(store, clone(o)); cb && cb(); }) } },
            tabs: { onRemoved: { addListener() {} } } } };
vm.createContext(bg);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), bg);
const acks = [];
const ev = (cs, ids) => ids.map((i) => ({ event_id: i, cs, type: "res_meta" }));
const send = (m, sender) => listener(m, sender, (r) => acks.push(r));
const main = { tab: { id: 7, url: "https://www.twitch.tv/x" }, frameId: 0, documentId: "D0" };
const sub = { tab: { id: 7, url: "https://www.twitch.tv/x" }, frameId: 3, documentId: "D3" };
(async () => {
  const drain = async () => { for (let i = 0; i < 400; i++) { await new Promise((r) => setImmediate(r)); if (q.length) q.shift()(); } };
  // two frames, interleaved: both messages arrive before either storage call ran
  send({ kind: "video-events-batch", payload: ev("A", [0, 1, 2]), cs: "A" }, main);
  send({ kind: "video-events-batch", payload: ev("B", [0, 1]), cs: "B" }, sub);
  send({ kind: "player-stats-batch", payload: [{ s: 1 }] }, main);
  send({ kind: "player-stats-batch", payload: [{ s: 2 }] }, main);
  await drain();
  // a resend of an already stored batch (its ack was lost) plus one new id
  send({ kind: "video-events-batch", payload: ev("A", [1, 2, 3]), cs: "A" }, main);
  // a batch carrying the instance's cumulative overflow drops
  send({ kind: "video-events-batch", payload: ev("B", [9]), cs: "B", dropped: { n: 6, first_id: 2, last_id: 8 } }, sub);
  await drain();
  fs.writeFileSync(process.argv[3], JSON.stringify({ store, acks }));
})();
"""

CS_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const timers = [];
const pending = [];                              // in-flight label batches: {m, cb}
let now = 1000;
const mkVideo = () => {
  const ls = {};
  return { paused: false, ended: false, readyState: 4, networkState: 2, currentTime: 0, videoWidth: 1280,
    videoHeight: 720, clientWidth: 853, clientHeight: 480, playbackRate: 1, duration: Infinity, isConnected: true,
    currentSrc: "blob:https://www.twitch.tv/1", srcObject: null, buffered: { length: 0 },
    getVideoPlaybackQuality: () => ({ droppedVideoFrames: 0, totalVideoFrames: 0 }),
    addEventListener(t, f) { (ls[t] = ls[t] || []).push(f); }, fire(t) { for (const f of ls[t] || []) f(); } };
};
const v = mkVideo();
const ctx = {
  console: { log() {} }, performance: { timeOrigin: 5e11, now: () => now },
  setInterval: (f) => timers.push(f), setTimeout: () => 0, clearInterval() {}, Date: { now: () => 5e11 + now },
  MutationObserver: class { observe() {} disconnect() {} },
  XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
  location: { href: "https://www.twitch.tv/x", hostname: "www.twitch.tv" },
  chrome: { runtime: { lastError: null, sendMessage: (m, cb) => {
    if (m.kind === "video-events-batch") pending.push({ m, cb }); else cb && cb({ ok: true }); } } },
  addEventListener() {}, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
};
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: {} }, body: {}, visibilityState: "visible",
  querySelectorAll: (q) => (q === "video" ? [v] : []), querySelector: () => null, evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__emit = emit; globalThis.__flush = tryFlush;" +
  "globalThis.__buf = () => eventBuffer.map((e) => e.event_id); globalThis.__dropped = () => ({ ...evDropped });" +
  "globalThis.__max = MAX_BUFFER_SIZE;", ctx);
const ids = (m) => m.payload.map((e) => e.event_id);
const out = {};
// the attach's video_start went out at load: ack it, the buffer is empty
let f = pending.shift();
f.cb({ ok: true, acked_max_id: Math.max(...ids(f.m)) });
out.cs = f.m.cs;
out.cs_on_events = f.m.payload.every((e) => e.cs === f.m.cs);
// 1. a batch in flight; the overflow shifts it out of the buffer before its ack lands
ctx.__emit("res_meta");
const inflight = pending.shift();
for (let i = 0; i < ctx.__max; i++) ctx.__emit("res_meta");
out.before_ack = ctx.__buf();
inflight.cb({ ok: true, acked_max_id: Math.max(...ids(inflight.m)) });
out.inflight_ids = ids(inflight.m);
out.after_ack = ctx.__buf();
out.dropped_after_ack = ctx.__dropped();
// 2. a full buffer, nothing in flight: the next event shifts an unsent one out (a loss, counted) and its flush carries it
ctx.__emit("res_meta");
const second = pending.shift();
out.second_dropped = second.m.dropped;
// 3. that flush fails after the overflow shifted one of its events out: that one is lost too, counted
ctx.__emit("res_meta");
second.cb(undefined);                             // no response: not stored
out.dropped_after_fail = ctx.__dropped();
ctx.__flush();
const third = pending.shift();
out.third_dropped = third.m.dropped;
out.third_ids = ids(third.m);
fs.writeFileSync(process.argv[3], JSON.stringify(out));
"""


def _node(harness: str, *files: Path) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found")
    with tempfile.TemporaryDirectory() as t:
        h, out = Path(t) / "h.js", Path(t) / "out.json"
        h.write_text(harness)
        subprocess.run([node, str(h), *map(str, files), str(out)], check=True, timeout=60)
        return json.loads(out.read_text())


def test_background_serializes_appends_records_frame_and_dedupes():
    r = _node(BG_HARNESS, LAB / "background.js")
    rows = r["store"]["events"]
    got = sorted((e["cs"], e["event_id"], e["frameId"], e["documentId"]) for e in rows)
    assert got == [("A", 0, 0, "D0"), ("A", 1, 0, "D0"), ("A", 2, 0, "D0"), ("A", 3, 0, "D0"),
                   ("B", 0, 3, "D3"), ("B", 1, 3, "D3"), ("B", 9, 3, "D3")], got
    assert sorted(x["s"] for x in r["store"]["player_stats"]) == [1, 2]
    ev_acks = [a for a in r["acks"] if "acked_max_id" in a]
    assert [a["acked_max_id"] for a in ev_acks] == [2, 1, 3, 9] and all(a["ok"] for a in r["acks"])
    nine = next(e for e in rows if e["cs"] == "B" and e["event_id"] == 9)
    assert (nine["dropped_n"], nine["dropped_first_id"], nine["dropped_last_id"]) == (6, 2, 8)
    assert all("dropped_n" not in e for e in rows if e is not nine)


def test_content_acks_by_id_and_counts_overflow_drops():
    r = _node(CS_HARNESS, LAB / "content.js")
    assert r["cs"] and r["cs_on_events"]
    k = max(r["inflight_ids"])
    assert len(r["inflight_ids"]) >= 1
    # the overflow shifted the in-flight events out; the ack drops only them, every unsent id stays
    assert r["before_ack"][0] == k + 1 and len(r["before_ack"]) == 1000
    assert r["after_ack"] == r["before_ack"], "an ack removed events that were never sent"
    assert r["dropped_after_ack"]["n"] == 0
    # an unsent event shifted out is a recorded drop, carried by the next batch
    assert r["second_dropped"] == {"n": 1, "first_id": k + 1, "last_id": k + 1}
    # the failed flush's shifted event is a loss too; cumulative per instance
    assert r["dropped_after_fail"] == {"n": 2, "first_id": k + 1, "last_id": k + 2}
    assert r["third_dropped"] == r["dropped_after_fail"]
    assert r["third_ids"] == list(range(k + 3, k + 1003))


class _FakeSW:
    """chrome.storage.local behind the export's CDP evals: events, player_stats and side:* keys in one store. levi 15:41
    (t100 F1): the paged side read (key list with sizes, then chunk gets) and 971e444e's single get(null) both answer
    from it; `none` names reads that get no answer (a CDP deadline: eval -> None), `drop(keys)` empties a chunk reply."""
    def __init__(self, session_dir: Path, events, stats, side=None):
        self.sd = session_dir
        self.store = {"events": events, "player_stats": stats, **(side or {})}
        self.cleared_with, self.removed, self.none, self.drop, self.gets = None, None, set(), None, []
        self.lists, self.after_list = 0, None         # after_list(n): runs after the n-th key list answered

    def _listed(self, reply):
        self.lists += 1
        if self.after_list:
            self.after_list(self.lists)
        return reply

    def _side(self):
        return {k: v for k, v in sorted(self.store.items()) if k.startswith("side:")}

    def eval(self, expr, await_promise=False):
        chunk_a, chunk_b = B._SIDE_CHUNK_JS.split("%s")
        if "remove(" in expr:                        # the files on disk at the FIRST clear
            if self.cleared_with is None:
                self.cleared_with = sorted(p.name for p in self.sd.iterdir())
                self.removed = json.loads(expr[expr.index(".concat(") + 8:expr.rindex("), r)")])
            return None
        if "{events:[]}" in expr:
            return None if "events" in self.none else json.dumps(self.store["events"])
        if "{player_stats:[]}" in expr:
            return None if "stats" in self.none else json.dumps(self.store["player_stats"])
        if expr == B._SIDE_LIST_JS:
            return None if "list" in self.none else self._listed(json.dumps(
                [[k, len(json.dumps(v, separators=(",", ":")))] for k, v in self._side().items()]))
        if expr == B._SIDE_KEYS_JS:
            return None if "keys" in self.none else self._listed(json.dumps([[k, None] for k in self._side()]))
        if expr == B._SIDE_LIST_ALL_JS:
            return None if "list_all" in self.none else self._listed(json.dumps(
                [[k, len(json.dumps(v, separators=(",", ":")))] for k, v in self._side().items()]))
        if expr.startswith(chunk_a) and expr.endswith(chunk_b):
            keys = json.loads(expr[len(chunk_a):-len(chunk_b)])
            self.gets.append(keys)
            if self.drop and self.drop(keys):
                return "{}"
            return json.dumps({k: self.store[k] for k in keys if k in self.store})
        if "Object.fromEntries" in expr:              # 971e444e's one read of every side key
            return None if "side_all" in self.none else json.dumps(self._side())
        if "getBytesInUse" in expr:
            return 0
        if "side_counters" in expr:
            return "null"
        return "{}"

    def close(self):
        pass


@pytest.fixture
def export_env(monkeypatch, tmp_path):
    monkeypatch.setattr(B.urllib.request, "urlopen", lambda *a, **k: types.SimpleNamespace(
        __enter__=lambda s: types.SimpleNamespace(read=lambda: b"[]"), __exit__=lambda *x: None))

    class _CM:
        def __enter__(self):
            return types.SimpleNamespace(read=lambda: b"[]")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(B.urllib.request, "urlopen", lambda *a, **k: _CM())
    monkeypatch.setattr(B, "_close_target", lambda *a: None)
    sw = _FakeSW(tmp_path, [{"ts": 1, "type": "res_meta", "event_id": 0, "cs": "A", "frameId": 0}], [{"s": 1}])
    monkeypatch.setattr(B, "_ext_page", lambda: (sw, None))
    return sw, tmp_path


def test_export_writes_files_before_the_storage_clear(export_env):
    sw, sd = export_env
    assert B._export_via_service_worker(sd) is True
    assert sw.cleared_with is not None and {"labels.csv", "player_stats.jsonl"} <= set(sw.cleared_with)
    assert not any(n.endswith(".tmp") for n in sw.cleared_with)
    head, row = (sd / "labels.csv").read_text().splitlines()
    assert head.split(",")[-6:] == ["cs", "frameId", "documentId", "dropped_n", "dropped_first_id", "dropped_last_id"]
    assert row.split(",")[-6:-4] == ['"A"', '"0"']


def test_a_failed_labels_write_leaves_storage_uncleared(export_env, monkeypatch):
    sw, sd = export_env
    real = B._write_durable

    def failing(path, text):
        if path.name == "labels.csv":
            raise OSError(28, "No space left on device")
        return real(path, text)
    monkeypatch.setattr(B, "_write_durable", failing)
    assert B._export_via_service_worker(sd) is False
    assert sw.cleared_with is None and not (sd / "labels.csv").exists()


def test_stop_posts_the_final_flush_before_the_export():
    src = (HERE / "browser.py").read_text()
    stop = src[src.index("    def stop(self) -> None:"):]
    assert stop.index("_final_flush(self._page)") < stop.index("_export_via_service_worker(self._session_dir)")
    assert B.FINAL_FLUSH_WAIT_S > 1.0                 # > content.js FLUSH_INTERVAL_MS: the iframes' timers land too
    assert "lvqoe-flush" in (LAB / "content.js").read_text()


# levi 09:23 (F1, boundary 3): meta.ext_export on every export exit, success too; when it is not ok, exp.py copies the
# clone's extension storage into the session dir before the clone is removed (a dead browser lost every record)
def _ext_export(sd):
    """The record as 971e444e wrote it; levi 14:51 (B2): plus exactly dur_ms and timeout_s, checked here."""
    rec = dict(json.loads((sd / "meta.json").read_text())["ext_export"])
    dur, to = rec.pop("dur_ms"), rec.pop("timeout_s")
    assert isinstance(dur, float) and dur >= 0 and to == B.CDP_CALL_TIMEOUT_S == 20
    return rec


def test_ext_export_is_recorded_on_success(export_env):
    sw, sd = export_env
    assert B._export_via_service_worker(sd) is True
    assert _ext_export(sd) == {"ok": True, "events": 1}


def test_ext_export_is_recorded_when_the_export_fails(export_env, monkeypatch):
    sw, sd = export_env
    monkeypatch.setattr(B, "_write_durable", lambda path, text: (_ for _ in ()).throw(OSError(28, "No space left")))
    assert B._export_via_service_worker(sd) is False
    rec = _ext_export(sd)
    assert rec["ok"] is False and "No space left" in rec["error"] and sw.cleared_with is None


def test_ext_export_is_recorded_when_no_extension_context(export_env, monkeypatch):
    sw, sd = export_env
    monkeypatch.setattr(B, "_ext_page", lambda: (None, None))
    assert B._export_via_service_worker(sd) is False
    assert _ext_export(sd) == {"ok": False, "reason": "no_ext_context"}


# levi 15:41 (t100 F1): a single get(null) of every side key answered nothing past a 64-128 MB store and the export said
# ok:true with no side file. The paged read (list, then byte-bounded chunks) and the completeness check, against
# 971e444e's export frozen in fixtures/export_via_sw_971e444e.py (git show 971e444e, the function verbatim).
def _side_store(n=12):
    side = {}
    for s in range(n):
        side[f"side:frames:{1_793_000_000_000 + s * 1000:015d}:{3 * s:09d}"] = [
            {"k": "tick", "fl": [[s, s + 0.5, 1280, 720]] * (s % 4 + 1)}, {"k": "mse", "ty": "app", "sb": 0, "t": s}]
        side[f"side:player_events:{1_793_000_000_000 + s * 1000 + 1:015d}:{3 * s + 1:09d}"] = [{"ev": "playing", "s": s}]
        if s % 5 == 0:
            side[f"side:zz_new:{1_793_000_000_000 + s * 1000 + 2:015d}:{3 * s + 2:09d}"] = [{"x": "é ü", "s": s}]
    return side


def _old_export(sd):
    """971e444e's _export_via_service_worker, run against this module's (monkeypatched) helpers."""
    ns = dict(vars(B))
    exec((HERE / "fixtures" / "export_via_sw_971e444e.py").read_text(), ns)
    return ns["_export_via_service_worker"](sd)


def _files(sd):
    return {p.name: p.read_bytes() for p in sd.iterdir() if p.is_file() and p.name != "meta.json"}


def _meta(sd):
    return json.loads((sd / "meta.json").read_text())


def test_fixture_is_971e444e_export():
    src = (HERE / "fixtures" / "export_via_sw_971e444e.py").read_text()
    assert src.startswith("def _export_via_service_worker(session_dir: Path) -> bool:")
    assert "Object.fromEntries(" in src and "_read_side_paged" not in src and "t0" not in src


@pytest.mark.parametrize("chunk_bytes", [B.SIDE_CHUNK_BYTES, 300])
def test_paged_side_files_are_byte_identical_to_971e444e(export_env, monkeypatch, tmp_path, chunk_bytes):
    sw, sd = export_env
    monkeypatch.setattr(B, "SIDE_CHUNK_BYTES", chunk_bytes)
    side = _side_store()
    sw.store.update(side)
    old_sd = tmp_path / "old"
    old_sd.mkdir()
    old = _FakeSW(old_sd, sw.store["events"], sw.store["player_stats"], side)
    with monkeypatch.context() as m:
        m.setattr(B, "_ext_page", lambda: (old, None))
        assert _old_export(old_sd) is True
    assert B._export_via_service_worker(sd) is True
    new_files, old_files = _files(sd), _files(old_sd)
    assert set(new_files) == set(old_files) >= {"labels.csv", "player_stats.jsonl", "rvfc_frames.jsonl",
                                                "player_events.jsonl"} and new_files == old_files
    assert sw.removed == old.removed == sorted(side)
    rd = _meta(sd)["ext_storage"]["side_read"]
    assert _meta(sd)["ext_storage"]["side_keys"] == _meta(sd)["ext_storage"]["side_keys_listed"] == len(side)
    assert rd["via"] == "sizes" and rd["failed"] == [] and "missing" not in rd and rd["chunks"] == len(sw.gets)
    assert (rd["chunks"] > 1) == (chunk_bytes == 300) and len(rd["chunk_ms"]) == rd["chunks"]
    assert [k for g in sw.gets for k in g] == sorted(side)       # every key read once, in key order


def test_chunks_are_bounded_by_bytes():
    sizes = [(f"k{i}", n) for i, n in enumerate([700_000, 900_000, 500_000, 2_500_000, 10, 1_999_990, 1])]
    chunks = B._side_chunks(sizes)
    assert [k for c in chunks for k in c] == [k for k, _ in sizes]
    n = dict(sizes)
    assert all(len(c) == 1 or sum(n[k] for k in c) <= B.SIDE_CHUNK_BYTES for c in chunks)
    assert chunks == [["k0", "k1"], ["k2"], ["k3"], ["k4", "k5"], ["k6"]]
    assert {len(c) for c in B._side_chunks([(f"k{i}", None) for i in range(20)])[:2]} == {B.SIDE_FALLBACK_KEYS}
    assert B._side_chunks([]) == []


def test_mutant_a_chunk_that_never_answers_fails_closed(export_env):
    """levi 15:41 mutant: a forced empty chunk -> ok:false (side_incomplete, n_expected, n_got), storage kept (so exp.py's
    F1 block keeps the raw store), labels.csv and the keys that did come back still written."""
    sw, sd = export_env
    side = _side_store()
    sw.store.update(side)
    lost = sorted(side)[3]
    sw.drop = lambda keys: lost in keys
    assert B._export_via_service_worker(sd) is False
    rec = _ext_export(sd)
    assert rec == {"ok": False, "reason": "side_incomplete", "n_expected": len(side), "n_got": len(side) - 1, "events": 1}
    assert sw.cleared_with is None and (sd / "labels.csv").is_file() and (sd / "rvfc_frames.jsonl").is_file()
    rd = _meta(sd)["ext_storage"]["side_read"]
    assert rd["missing"] == {"n": 1, "first": [lost]} and rd["failed"][0]["got"] == 0


def test_null_971e444e_reports_ok_with_no_side_file_when_the_read_gets_no_answer(export_env, monkeypatch):
    """The defect as 971e444e has it: its one side read answers None (the 128 MB synthetic), the export says ok:true,
    writes no side file and clears storage. The paged read under the same store answers every key."""
    sw, sd = export_env
    sw.store.update(_side_store())
    sw.none.add("side_all")
    assert _old_export(sd) is True
    assert _meta(sd)["ext_export"] == {"ok": True, "events": 1} and sw.cleared_with is not None
    assert not (sd / "rvfc_frames.jsonl").exists()


def test_a_chunk_that_answers_short_once_is_reread_key_by_key(export_env):
    sw, sd = export_env
    side = _side_store()
    sw.store.update(side)
    calls = []
    sw.drop = lambda keys: (calls.append(1), len(calls) == 1)[1]          # the first chunk reply only
    assert B._export_via_service_worker(sd) is True
    assert _ext_export(sd) == {"ok": True, "events": 1} and sw.removed == sorted(side)
    rd = _meta(sd)["ext_storage"]["side_read"]
    assert rd["failed"] == [{"i": 0, "keys": len(side), "got": 0, "ms": rd["chunk_ms"][0]}]
    assert rd["chunks"] == 1 + len(side) and "missing" not in rd


def test_an_unreadable_key_list_falls_back_or_fails_closed(export_env):
    sw, sd = export_env
    side = _side_store()
    sw.store.update(side)
    sw.store["events"] = []                       # the no-events path checks completeness too
    sw.none.add("list")
    assert B._export_via_service_worker(sd) is False
    assert _ext_export(sd) == {"ok": True, "events": 0} and sw.removed == sorted(side)
    assert _meta(sd)["ext_storage"]["side_read"]["via"] == "keys"
    sw.none.add("keys")                           # Chrome < 130: no getKeys, the get(null) list
    sd2 = sd / "nokeys"
    sd2.mkdir()
    sw.sd, sw.cleared_with = sd2, None
    assert B._export_via_service_worker(sd2) is False
    assert _ext_export(sd2) == {"ok": True, "events": 0} and _meta(sd2)["ext_storage"]["side_read"]["via"] == "sizes_all"
    sd3 = sd / "again"
    sd3.mkdir()
    sw.sd, sw.cleared_with = sd3, None
    sw.none.add("list_all")
    assert B._export_via_service_worker(sd3) is False
    assert _ext_export(sd3) == {"ok": False, "reason": "side_incomplete", "n_expected": 0, "n_got": 0, "events": 0}
    assert sw.cleared_with is None and _meta(sd3)["ext_storage"]["side_read"]["via"] is None


def _late_keys():
    return {"side:frames:1793000099000:000000901": [{"k": "tick", "fl": [[99, 99.5, 1280, 720]]}],
            "side:zz_late:1793000099001:000000902": [{"late": 1}]}


def test_a_key_written_after_the_list_is_exported_not_lost(export_env):
    """levi 15:58: a flush that lands between the first list and the clear (frames, and a stream not seen before)
    is read by the re-list, written to its stream's file and only then cleared; n_late on record."""
    sw, sd = export_env
    side = _side_store()
    sw.store.update(side)
    sw.after_list = lambda n: sw.store.update(_late_keys()) if n == 1 else None
    assert B._export_via_service_worker(sd) is True
    assert _ext_export(sd) == {"ok": True, "events": 1}
    assert sw.removed == sorted({**side, **_late_keys()})
    frames = [json.loads(x) for x in (sd / "rvfc_frames.jsonl").read_text().splitlines()]
    assert frames[-1] == {"k": "tick", "fl": [[99, 99.5, 1280, 720]]}
    assert frames[:-1] == [r for k in sorted(side) if k.startswith("side:frames:") for r in side[k]]
    assert (sd / "side_zz_late.jsonl").read_text() == '{"late": 1}\n'
    m = _meta(sd)
    assert m["ext_storage"]["side_read"]["n_late"] == 2 and m["ext_storage"]["side_keys"] == len(side) + 2
    assert m["ext_storage"]["side_keys_listed"] == len(side) + 2
    assert {u["stream"] for u in m["side_streams_unmapped"]} == {"zz_new", "zz_late"}


def test_a_late_key_that_cannot_be_read_fails_closed(export_env):
    sw, sd = export_env
    sw.store.update(_side_store())
    sw.after_list = lambda n: sw.store.update(_late_keys()) if n == 1 else None
    sw.drop = lambda keys: "side:zz_late:1793000099001:000000902" in keys
    assert B._export_via_service_worker(sd) is False
    rec = _ext_export(sd)
    assert (rec["reason"], rec["n_expected"] - rec["n_got"]) == ("side_incomplete", 1) and sw.cleared_with is None
    assert _meta(sd)["ext_storage"]["side_read"]["relist"]["missing"]["n"] == 1


@pytest.mark.parametrize("which,reason", [("events", "events_read_failed"), ("stats", "stats_read_failed")])
def test_an_events_or_stats_read_with_no_answer_fails_closed(export_env, which, reason, tmp_path):
    """971e444e read a None events reply as "no events", cleared storage and said ok:true; now ok:false, kept."""
    sw, sd = export_env
    sw.none.add(which)
    if which == "events":
        old_sd = tmp_path / "old"
        old_sd.mkdir()
        sw.sd = old_sd
        assert _old_export(old_sd) is False
        assert _meta(old_sd)["ext_export"] == {"ok": True, "events": 0} and sw.cleared_with is not None   # the null
        sw.sd, sw.cleared_with = sd, None
    assert B._export_via_service_worker(sd) is False
    assert _ext_export(sd) == {"ok": False, "reason": reason} and sw.cleared_with is None


def test_keep_ext_storage_copies_only_the_extension_storage(tmp_path):
    from profile_clone import EXT_STORAGE_COPY, keep_ext_storage
    prof, sd = tmp_path / "clone", tmp_path / "session"
    sd.mkdir()
    assert keep_ext_storage(prof, sd) == {"copied": False, "reason": "absent"}
    db = prof / "Default" / "Local Extension Settings" / "abcdefghijklmnopabcdefghijklmnop"
    db.mkdir(parents=True)
    (db / "000003.log").write_bytes(b"side:frames:...")
    (db / "MANIFEST-000001").write_bytes(b"m")
    (prof / "Default" / "Cookies").write_text("session cookies")
    rec = keep_ext_storage(prof, sd)
    assert rec == {"copied": True, "dir": EXT_STORAGE_COPY, "files": 2, "bytes": 16}
    assert (sd / EXT_STORAGE_COPY / db.name / "000003.log").read_bytes() == b"side:frames:..."
    assert not any(p.name == "Cookies" for p in sd.rglob("*"))


def test_exp_keeps_the_raw_storage_after_chrome_is_killed_and_before_the_clone_goes():
    src = (HERE / "exp.py").read_text()
    fin = src[src.index("    finally:\n        # finalising"):]
    kill, keep = fin.index("kill_chrome(chrome_profile)"), fin.index("keep_ext_storage(chrome_profile")
    remove = fin.index("remove_profile_clone(CURRENT_DATA / 'chrome_profiles', chrome_profile)", keep)
    check = fin.index("check_session(session_dir)")
    assert kill < keep < check < remove                 # FIN 13:30: the keep before check_session moves a FAIL
    assert 'ext_export.get("ok") is not True' in fin and '"reason": "not_run"' in fin


def _f1_block():
    """exp.py's own F1 block (between its markers), run as exp.py's finally runs it."""
    import textwrap
    src = (HERE / "exp.py").read_text()
    a = src.index("\n", src.index("F1 block begin")) + 1
    return textwrap.dedent(src[a:src.index("# F1 block end", a)])


def _f1_session(root: Path, ok: bool):
    """A session dir under root/data/youtube with meta.ext_export {ok}, and a profile clone holding extension storage."""
    sd = root / "data" / "youtube" / "yt_1"
    sd.mkdir(parents=True)
    (sd / "meta.json").write_text(json.dumps({"ext_export": {"ok": ok, **({} if ok else {"reason": "error"})}}))
    prof = root / "clone"
    db = prof / "Default" / "Local Extension Settings" / "abcdefghijklmnopabcdefghijklmnop"
    db.mkdir(parents=True)
    (db / "000003.log").write_bytes(b"side:frames:...")
    return sd, prof


def _run_f1(sd: Path, prof: Path) -> str:
    import contextlib
    import io
    import session_meta
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(_f1_block(), {"json": json, "session_meta": session_meta, "session_dir": sd, "chrome_profile": prof})
    return out.getvalue()


def _check_session_moving(sd: Path) -> Path:
    """The real check_session.py --move-failed, as exp.check_session runs it; the dir it ends in."""
    subprocess.run([sys.executable, str(HERE / "check_session.py"), str(sd), "--move-failed"], check=False,
                   capture_output=True, timeout=300)
    moved = sd.parent.parent / "_failed" / sd.parent.name / sd.name
    return moved if moved.is_dir() else sd


def test_f1_keeps_the_raw_storage_when_check_session_moves_the_failed_dir(tmp_path):
    """FIN 13:30 (yt2 killed at 54ed22cd: "ext storage not kept: FileNotFoundError"): with the F1 block before
    check_session, a failed export's storage is in _failed/<platform>/<session>/ext_storage_raw."""
    sd, prof = _f1_session(tmp_path, ok=False)
    assert "not kept" not in _run_f1(sd, prof)
    final = _check_session_moving(sd)
    assert final == tmp_path / "data" / "_failed" / "youtube" / "yt_1" and not sd.exists()
    assert (final / "ext_storage_raw" / "abcdefghijklmnopabcdefghijklmnop" / "000003.log").read_bytes() == b"side:frames:..."
    rec = json.loads((final / "meta.json").read_text())["ext_export"]
    assert rec["ok"] is False and rec["raw"]["copied"] is True and rec["raw"]["files"] == 1


def test_f1_mutant_54ed22cd_order_loses_the_raw_storage(tmp_path):
    """The mutant (54ed22cd: check_session first, then the F1 block on the old path) keeps nothing."""
    sd, prof = _f1_session(tmp_path, ok=False)
    final = _check_session_moving(sd)
    assert final != sd
    log = _run_f1(sd, prof)
    assert "ext storage not kept: FileNotFoundError" in log, log
    assert not any(p.name == "ext_storage_raw" for p in tmp_path.rglob("*"))


def test_f1_ok_export_and_unmoved_dir_are_unchanged(tmp_path):
    """An ok export copies nothing and leaves meta as it was; a failed export in a dir nobody moves (the PASS path's
    place) is kept in place, as at 54ed22cd."""
    sd, prof = _f1_session(tmp_path / "a", ok=True)
    before = (sd / "meta.json").read_text()
    _run_f1(sd, prof)
    assert not (sd / "ext_storage_raw").exists() and (sd / "meta.json").read_text() == before
    sd, prof = _f1_session(tmp_path / "b", ok=False)
    _run_f1(sd, prof)
    assert (sd / "ext_storage_raw" / "abcdefghijklmnopabcdefghijklmnop" / "000003.log").is_file()
    assert json.loads((sd / "meta.json").read_text())["ext_export"]["raw"]["copied"] is True
