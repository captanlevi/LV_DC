#!/usr/bin/env python3
"""tt anchor A/B (levi 00144 A1-A4; supervisor 00:36 b3): the worker anchor's arm selector and the proxy install.

  - the arm: the constant by default (TT_WORKER_ANCHOR / TW_WORKER_ANCHOR = cbe3ce9d's "full"); the dry cycle
    (LVDC_ANCHOR_AB_TT / _TW) gives cycle[session number % length], recorded (meta ab_arm); a counted session keeps the
    constant whatever the env says (mutant: an override honoured in a counted run); anchor_ab_refusal refuses the env on a
    counted run and an unknown arm; exp.py / orchestrate.py call it before any session dir;
  - full is cbe3ce9d's script, byte for byte; off attaches nothing; b1 attaches and resumes, installs nothing, never takes;
  - proxy (b3) under node, the same plan as test_worker_anchor's: the same records and checks as full (the rows are the
    reason for b3), the install returns its take function (no worker global, no "again" flag), and every wrapped method /
    the timestampOffset setter prints its original text through Function.prototype.toString / String(), keeps name /
    length / property descriptor shape, and toString prints its own native text (mutant: full's plain wrapper leaks);
  - proxy's CDP: the install evaluates without returnByValue, the take is Runtime.callFunctionOn on the returned objectId;
  - G0 (A4): constant = X with no override and override = X give the same arm, the same injected script and the same CDP
    command sequence;
  - --session-range-tt: tiktok slots only, refused with --trio-record or without --concurrent.

    python3 test_anchor_ab.py        (needs node; exit 0 = pass)
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
import test_worker_anchor as W  # noqa: E402

# test_worker_anchor's harness, reading the take function the proxy install returns (else the full global), running the
# install once for proxy (it has no "again" flag), and recording what each wrapped function prints / names / declares;
# the stub methods take the context's Function.prototype (a worker has one realm, so String(f) reaches the shim)
HARNESS = (W.HARNESS
           .replace('const out = { first: vm.runInContext(src, ctx), second: vm.runInContext(src, ctx), checks: {} };',
                    'const look = () => { const P = SourceBuffer.prototype, M = MediaSource.prototype, r = {};\n'
                    '  const fs_ = { appendBuffer: P.appendBuffer, remove: P.remove, abort: P.abort, changeType: P.changeType,\n'
                    '    addSourceBuffer: M.addSourceBuffer, tso_set: Object.getOwnPropertyDescriptor(P, "timestampOffset").set };\n'
                    '  for (const [k, f] of Object.entries(fs_)) r[k] = { ts: vm.runInContext("Function.prototype.toString", ctx).call(f),\n'
                    '    str: vm.runInContext("String", ctx)(f), name: f.name, length: f.length };\n'
                    '  for (const k of ["appendBuffer", "remove", "abort", "changeType"]) { const d = Object.getOwnPropertyDescriptor(P, k);\n'
                    '    r[k].desc = [d.writable, d.enumerable, d.configurable, typeof d.value]; }\n'
                    '  const d = Object.getOwnPropertyDescriptor(P, "timestampOffset"); r.tso_desc = [d.enumerable, d.configurable, typeof d.get, typeof d.set];\n'
                    '  const T = vm.runInContext("Function.prototype.toString", ctx); r.toString_self = T.call(T); r.toString_name = T.name;\n'
                    '  r.globals = vm.runInContext("Object.getOwnPropertyNames(self).filter(k => k.startsWith(\\"__lvqoe\\"))", ctx);\n'
                    '  return r; };\n'
                    '{ const FP = vm.runInContext("Function.prototype", ctx), P = SourceBuffer.prototype;   // one realm, as a worker\n'
                    '  for (const f of [P.appendBuffer, P.remove, P.abort, P.changeType, MediaSource.prototype.addSourceBuffer,\n'
                    '    Object.getOwnPropertyDescriptor(P, "timestampOffset").set, Object.getOwnPropertyDescriptor(P, "timestampOffset").get])\n'
                    '    Object.setPrototypeOf(f, FP); }\n'
                    'const before = look();\n'
                    'const first = vm.runInContext(src, ctx);\n'
                    'const out = { first: typeof first === "function" ? "fn" : first,\n'
                    '  second: typeof first === "function" ? null : vm.runInContext(src, ctx), checks: {}, before, after: look() };\n'
                    'const takeFn = typeof first === "function" ? first : () => ctx.__lvqoeAnchorTake();')
           .replace('out.take = JSON.parse(ctx.__lvqoeAnchorTake());\nout.take2 = JSON.parse(ctx.__lvqoeAnchorTake());',
                    'out.take = JSON.parse(takeFn());\nout.take2 = JSON.parse(takeFn());'))


def run_js(steps: list, js: str) -> dict:
    node = shutil.which("node")
    assert node, "node not found: the anchor A/B test cannot run (install node)"
    with tempfile.TemporaryDirectory() as t:
        h, src, plan, out = (Path(t) / x for x in ("h.js", "w.js", "plan.json", "out.json"))
        h.write_text(HARNESS)
        src.write_text(js)
        plan.write_text(json.dumps({"now_ms": 1_000_000, "steps": steps}))
        subprocess.run([node, str(h), str(src), str(plan), str(out)], check=True)
        return json.loads(out.read_text())


def plan() -> list:
    v = [W.sample([W.nal(5 if i == 0 else 1, 300, False, W.random.Random(i))]) for i in range(40)]
    steps = [{"op": "add", "mime": 'video/mp4;codecs="hvc1.1.6.L93.B0"'}, {"op": "add", "mime": 'audio/mp4;codecs="mp4a.40.2"'},
             {"op": "app", "sb": 0, "hex": W.init_seg(W.avc1(720, 1280), with_audio=False).hex()},
             {"op": "app", "sb": 1, "hex": (W.init_seg(W.avc1(720, 1280)) + W.frag(2, 0, [bytes(10)] * 3, 1024)).hex()}]
    for i in range(40):
        steps.append({"op": "app", "sb": 0, "hex": W.frag(1, i * 40, [v[i]], 40, [0]).hex()})
        if i == 20:
            steps += [{"op": "ct", "sb": 0, "mime": 'video/mp4;codecs="avc1.64001f"'}, {"op": "tso", "sb": 0, "v": 3.25}]
    steps += [{"op": "rm", "sb": 0, "s": -1, "e": 5}, {"op": "abort", "sb": 0},
              {"op": "end", "sb": 0, "ranges": [[0, 1.6]]}, {"op": "evil", "sb": 0}]
    return steps


def test_proxy_rows_equal_full_and_native_text():
    steps = plan()
    full, prox = run_js(steps, B.worker_anchor_js(0)), run_js(steps, B.worker_anchor_js(0, proxy=True))
    assert full["first"] == "ok" and full["second"] == "again" and prox["first"] == "fn" and prox["second"] is None
    assert prox["checks"] == full["checks"] == {"add_same": True, "append_same": True, "tso_set": True,
                                               "throw_same": True, "fault_contained": True}, (prox["checks"], full["checks"])
    assert prox["take"] == full["take"] and prox["take2"] == full["take2"] and prox["n_calls"] == full["n_calls"]
    assert {r["op"] for r in prox["take"]["recs"]} >= {"add", "init", "app", "aapp", "ct", "tso", "rm", "abort", "end"}
    # b3's point: what a page can read off the wrapped functions is what it read before the install
    for k in ("appendBuffer", "remove", "abort", "changeType", "addSourceBuffer", "tso_set"):
        assert prox["after"][k] == prox["before"][k], (k, prox["before"][k], prox["after"][k])
    assert prox["after"]["tso_desc"] == prox["before"]["tso_desc"]
    assert prox["after"]["toString_self"] == prox["before"]["toString_self"] == "function toString() { [native code] }"
    assert prox["after"]["toString_name"] == "toString" and prox["after"]["globals"] == []
    # the mutant: full's plain wrapper prints its own source, and leaves its globals
    assert full["after"]["appendBuffer"]["ts"] != full["before"]["appendBuffer"]["ts"]
    assert "lvqoe" not in full["after"]["appendBuffer"]["ts"] and "before(this, a)" in full["after"]["appendBuffer"]["ts"]
    assert full["after"]["globals"] == ["__lvqoeAnchor", "__lvqoeAnchorTake"]


def test_full_is_cbe3ce9d_and_proxy_edits_apply_once():
    js = B.worker_anchor_js(0)
    assert "self.__lvqoeAnchorTake = () =>" in js and "new Proxy" not in js
    for old, _ in B.ANCHOR_PROXY_EDITS:
        assert js.count(old) == 1, old[:60]
    p = B.worker_anchor_js(60, proxy=True)
    assert "__lvqoe" not in p and p.count("new Proxy(") == 3 and "return take;" in p and "BLOCK_S = 60" in p


def test_arm_selection_and_counted_mutant():
    keep = B.TT_WORKER_ANCHOR
    try:
        assert B.anchor_arm("tiktok", {}, "ti_100183", False) == {"arm": "full", "source": "constant"}
        assert B.anchor_arm("twitch", {}, "tw_100183", False) == {"arm": "full", "source": "constant"}
        env = {"LVDC_ANCHOR_AB_TT": "off,proxy,b1,full,off,proxy,b1,full,off,proxy,b1"}
        cyc = env["LVDC_ANCHOR_AB_TT"].split(",")
        got = [B.anchor_arm("tiktok", env, f"ti_{n}", False)["arm"] for n in range(100183, 100197)]
        assert got == [cyc[n % 11] for n in range(100183, 100197)]
        assert B.anchor_arm("tiktok", env, "ti_100183", False) == {"arm": cyc[100183 % 11], "source": "env",
                                                                   "env": env["LVDC_ANCHOR_AB_TT"], "n": 100183}
        assert B.anchor_arm("twitch", env, "tw_100183", False)["source"] == "constant"     # tt's env is tt's only
        # a counted session keeps the constant, the env named; the mutant (the override honoured) fails here
        assert B.anchor_arm("tiktok", {"LVDC_ANCHOR_AB_TT": "off"}, "ti_7", True) == {"arm": "full", "source": "constant",
                                                                                      "env_ignored": "off"}
        B.TT_WORKER_ANCHOR = "proxy"
        assert B.anchor_arm("tiktok", {}, "ti_7", True) == {"arm": "proxy", "source": "constant"}
    finally:
        B.TT_WORKER_ANCHOR = keep
    assert B.anchor_ab_refusal({}, True) is None and B.anchor_ab_refusal({"LVDC_ANCHOR_AB_TT": "off,full"}, False) is None
    assert "counted run" in B.anchor_ab_refusal({"LVDC_ANCHOR_AB_TT": "off"}, True)
    assert "counted run" in B.anchor_ab_refusal({"LVDC_ANCHOR_AB_TW": "full"}, True)
    assert "not in" in B.anchor_ab_refusal({"LVDC_ANCHOR_AB_TT": "off,on"}, False)
    ex, orch = (HERE / "exp.py").read_text(), (HERE / "orchestrate.py").read_text()
    i_ref, i_dir = ex.index("_abr = anchor_ab_refusal(os.environ"), ex.index("    if args.save_dir:\n        session_dir = args.save_dir")
    assert i_ref < i_dir and "sys.exit(EXIT_ANCHOR_AB_REFUSED)" in ex and 'meta_extra["ab_arm"] = browser.ab_arm' in ex
    import orchestrate as O
    assert set(O.ANCHOR_AB_ENVS) == set(B.ANCHOR_AB_ENV.values()) and "import browser" not in orch.split("def arg_refusal")[1][:4000]
    assert "with --trio-record: the anchor A/B is dry only" in (HERE / "test_arg_refusal.py").read_text()


class _Fake:
    _ws_url = "ws://x"
    _anchor = None
    platform = "tiktok"


def test_off_attaches_nothing_and_b1_installs_nothing():
    f = _Fake()
    f.frame_log, f.ab_arm = {"enabled": True, "block_s": 0}, {"arm": "off", "source": "env"}
    B.BrowserSession._worker_anchor_install(f)
    assert f._anchor is None and f.frame_log["worker_anchor"] == {"arm": "off"}
    a, s = B.WorkerAnchor("ws://x", 0, "b1"), W.FakeSend()
    a.handle(W.attach(), s, now=1.0)
    assert [m for _, m, _, _ in s.sent] == ["Runtime.runIfWaitingForDebugger"] and a.workers[0]["wrap"] == "none (b1)"
    a.take(s)
    assert len(s.sent) == 1 and a.record()["arm"] == "b1"


def test_proxy_cdp_take_by_object_id():
    a, s = B.WorkerAnchor("ws://x", 0, "proxy"), W.FakeSend()
    a.handle(W.attach("S1"), s, now=1.0)
    _, m, prm, sid = s.sent[0]
    assert m == "Runtime.evaluate" and prm["returnByValue"] is False and prm["expression"] == B.worker_anchor_js(0, proxy=True)
    a.handle({"id": 1, "result": {"result": {"type": "function", "objectId": "OID-1"}}}, s, now=1.01)
    assert a.workers[0]["wrap"] == "ok"
    a.take(s)
    assert s.sent[-1][1:] == ("Runtime.callFunctionOn", {"functionDeclaration": B.ANCHOR_TAKE_FN, "objectId": "OID-1",
                                                         "returnByValue": True}, "S1")
    batch = {"recs": [{"op": "ct", "sb": 1, "mime": "video/mp4;codecs=avc1", "t": 1}], "over": 0, "inst": 1, "first": None,
             "ms": 0.1, "nv": 0, "to": 1.5}
    a.handle({"id": s.sent[-1][0], "result": {"result": {"value": json.dumps(batch)}}}, s, now=2.0)
    assert s.sent[-1][1] == "Runtime.evaluate" and s.sent[-1][3] is None and a.workers[0]["recs"] == 1
    a2, s2 = B.WorkerAnchor("ws://x", 0, "proxy"), W.FakeSend()          # an install that threw: no objectId, never taken
    a2.handle(W.attach("S2"), s2, now=1.0)
    a2.handle({"id": 1, "result": {"result": {"type": "object"}, "exceptionDetails": {"text": "Uncaught"}}}, s2, now=1.01)
    a2.take(s2)
    assert a2.workers[0]["wrap"] is None and "Uncaught" in a2.workers[0]["wrap_error"] and len(s2.sent) == 2


def _cdp_sequence(arm: str) -> list:
    a, s = B.WorkerAnchor("ws://x", 0, arm), W.FakeSend()
    a.handle(W.attach("S1"), s, now=1.0)
    if arm == "proxy":
        a.handle({"id": 1, "result": {"result": {"objectId": "O"}}}, s, now=1.01)
    elif arm == "full":
        a.handle({"id": 1, "result": {"result": {"value": "ok"}}}, s, now=1.01)
    a.take(s)
    return [(m, json.dumps(p, sort_keys=True), sid) for _, m, p, sid in s.sent]


def test_g0_constant_equals_override():
    """levi 00144 A4: constant = X (no override) and override = X give the same arm, script and CDP sequence."""
    keep = B.TT_WORKER_ANCHOR
    try:
        for x in ("full", "proxy", "off"):
            B.TT_WORKER_ANCHOR = x
            c = B.anchor_arm("tiktok", {}, "ti_100190", False)["arm"]
            B.TT_WORKER_ANCHOR = "full"
            o = B.anchor_arm("tiktok", {"LVDC_ANCHOR_AB_TT": x}, "ti_100190", False)["arm"]
            assert c == o == x
            if x != "off":
                assert B.WorkerAnchor("ws://x", 0, c).js == B.WorkerAnchor("ws://x", 0, o).js
                assert _cdp_sequence(c) == _cdp_sequence(o)
    finally:
        B.TT_WORKER_ANCHOR = keep
    assert B.WorkerAnchor("ws://x", 0).js == B.worker_anchor_js(0) and _cdp_sequence("full")[0][0] == "Runtime.evaluate"


def test_session_range_tt():
    import orchestrate as O
    src = (HERE / "orchestrate.py").read_text()
    assert 'rng = SESSION_RANGE_TT if (p == "tiktok" and SESSION_RANGE_TT) else SESSION_RANGE' in src
    assert '"--session-range-tt"' in src and 'for flag, val in (("--session-range", args.session_range), ("--session-range-tt", args.session_range_tt)):' in src
    assert O.SESSION_RANGE_TT is None


def main() -> int:
    for f in (test_proxy_rows_equal_full_and_native_text, test_full_is_cbe3ce9d_and_proxy_edits_apply_once,
              test_arm_selection_and_counted_mutant, test_off_attaches_nothing_and_b1_installs_nothing,
              test_proxy_cdp_take_by_object_id, test_g0_constant_equals_override, test_session_range_tt):
        f()
    print("test_anchor_ab: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
