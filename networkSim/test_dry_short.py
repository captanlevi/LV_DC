#!/usr/bin/env python3
"""The short per-item dry runs (LVQoE restart_2026-10-21_prereg_levi.md, "Short per-item dry runs"; ops/diag/
dry_minimums.py, trio_dry_run --short, decode_stress --short): each gate's minimum support, exactly at it sufficient
and below it "insufficient", never a pass; a fail stays a fail; the readers carry the check (goodput, the top-state
capture segment, the bulk ratio, @fast, the player load, no leak, decode stress); the short profile meets every
minimum; @fast auto-sized from its seeded plans; orchestrate --trio-stress --episodes for decode_stress --minutes.
No root, no network.

    python3 test_dry_short.py        (exit 0 = pass)
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("LVDC_MGMT_UDP_PORT", "41641")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name):
    sp = importlib.util.spec_from_file_location(name, HERE.parent / "ops" / "diag" / f"{name}.py")
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


M = _load("dry_minimums")
T = _load("trio_dry_run")
D = _load("decode_stress")


def test_each_minimum_at_and_below():
    """levi's table: exactly the minimum is sufficient, a hair below it is not."""
    assert M.goodput(30.0)["sufficient"] and not M.goodput(29.9)["sufficient"] and not M.goodput(None)["sufficient"]
    assert M.capture_drops(60.0)["sufficient"] and not M.capture_drops(59.9)["sufficient"]
    assert M.bulk_ratio([30.0, 30.0, 30.0])["sufficient"]
    assert not M.bulk_ratio([30.0, 30.0, 29.9])["sufficient"] and not M.bulk_ratio([40.0, 40.0])["sufficient"]
    assert not M.bulk_ratio([30.0, None, 30.0])["sufficient"]
    assert M.fast(30)["sufficient"] and not M.fast(29)["sufficient"]
    assert M.walk(120.0, 30, 20.0)["sufficient"]
    for args in ((119.9, 30, 20.0), (120.0, 29, 20.0), (120.0, 30, 19.9)):
        assert not M.walk(*args)["sufficient"], args
    assert M.player_a(60.0, [10.0, 10.0, 10.0])["sufficient"]
    assert not M.player_a(59.9, [10.0, 10.0, 10.0])["sufficient"] and not M.player_a(60.0, [10.0, 10.0, 9.9])["sufficient"]
    assert not M.player_a(60.0, [10.0, 10.0])["sufficient"]
    assert M.h3(20)["sufficient"] and not M.h3(19)["sufficient"] and M.h3(20)["gate"] == "h3_exercised"
    assert M.no_leak({"yt1": 1, "tw1": 3})["sufficient"]
    assert not M.no_leak({"yt1": 1, "tw1": 0})["sufficient"] and not M.no_leak({})["sufficient"]
    # levi (prereg 40843f81): the seconds with every slot at its top shaping state, all running, at any tier
    assert M.decode_stress(300.0)["sufficient"] and M.decode_stress(300.0)["support"] == {"all_top_state_s": 300.0}
    assert not M.decode_stress(299.9)["sufficient"] and not M.decode_stress(None)["sufficient"]
    c = M.goodput(12.0)
    assert c["minimum"] == {"seconds": 30.0} and c["support"] == {"seconds": 12.0} and c["gate"] == "goodput"


def test_status_never_passes_below_the_minimum():
    ok, short = M.goodput(30.0), M.goodput(29.0)
    assert M.status(True, ok) == "pass" and M.status("pass", ok) == "pass"
    assert M.status(True, short) == "insufficient" and M.status("pass", short) == "insufficient"
    assert M.status(False, short) == "fail" and M.status("fail (not read)", short) == "fail"   # a fault is never hidden
    assert M.status("insufficient", ok) == "insufficient" and M.status(False, ok) == "fail"
    assert M.combine(["pass", "pass"]) == "pass" and M.combine(["pass", "insufficient"]) == "insufficient"
    assert M.combine(["insufficient", "fail"]) == "fail" and M.combine([]) == "insufficient"


def test_the_short_profile_meets_every_minimum():
    S, m = M.SHORT, M.MINIMUMS
    assert S["goodput_s"] >= m["goodput"]["seconds"] and S["top_capture_s"] >= m["capture_drops"]["seconds_all_top"]
    # the bulk read window is the state less 3 s and 0.5 s, its sampled span up to ~1 s shorter at each end
    assert S["state_s"] - 3.5 - 2.0 >= m["bulk_ratio"]["non_straddling_s"]
    assert S["player_state_s"] >= m["player_a"]["state_s"] and S["fast_changes"] >= m["fast"]["changes"]
    # the all-top window opens late at N = 5 (stagger + tt's lead-in) and closes at the first slot's end; 30 s margin
    assert M.DECODE_SHORT_MINUTES * 60 - M.DECODE_SHORT_START_S >= m["decode_stress"]["all_top_state_s"] + 30.0
    n = M.short_total_s(S, 3, {"goodput": True, "top_capture": True})
    assert n == {"goodput": 35.0, "bulk": 120.0, "top_capture": 69.0, "total": 224.0}, n
    full = M.short_total_s(S, 3, {"goodput": True, "idle_ping": True, "top_capture": True, "fast": 140.0, "player": True})
    assert 300.0 <= full["total"] <= 660.0, full                          # every phase at once: about 10 min


def test_resolve_profile():
    def ns(**k):
        base = dict(short=False, state_s=None, goodput_s=None, ping_s=None, top_capture_s=None, player_state_s=None,
                    single_reference=False, no_single_reference=False)
        return types.SimpleNamespace(**{**base, **k})
    a = ns(short=True)
    T.resolve_profile(a)
    assert (a.state_s, a.goodput_s, a.ping_s, a.top_capture_s, a.player_state_s) == (40.0, 35.0, 10.0, 66.0, 65.0)
    assert a.no_single_reference is True
    a = ns(short=True, state_s=50.0, single_reference=True)
    T.resolve_profile(a)
    assert a.state_s == 50.0 and a.no_single_reference is False                          # given values stand
    a = ns()
    T.resolve_profile(a)
    # levi: the full set runs the capture-drops segment at every N
    assert (a.state_s, a.goodput_s, a.ping_s, a.top_capture_s, a.player_state_s) == (60.0, 60.0, 20.0, 66.0, 60.0)
    assert a.no_single_reference is False
    a = ns(state_s=75.0)
    T.resolve_profile(a)
    assert a.player_state_s == 75.0                                       # full form: the player's state is the bulk's
    args = ["--short", "--print-only"]
    assert T.main(args) == 0                                             # parses (prints the tree only)


def test_fast_auto_sized_from_the_seeded_plans():
    F = _load("fast_segment")
    secs = T.fast_auto_s(32, 0)
    for s in T.SESSIONS:
        plan = F.schedule(T.platform_of(s), s, secs, 0)
        assert sum(1 for e in plan[1:] if not e.get("walk_step")) >= 32, (s, len(plan), secs)   # re-draws (§2)
        assert plan == F.schedule(T.platform_of(s), s, 900.0, 0)[:len(plan)]     # a prefix of the long horizon
    assert 32 * 2.4 < secs < 32 * 6.0 + 2.0, secs
    try:
        M.fast_seconds({"x": [{"t": 0.0}, {"t": 3.0}]}, 5)
        raise AssertionError("a short plan must raise")
    except ValueError:
        pass


def test_top_capture_reading():
    assert T.TCPDUMP_B_KIB == __import__("trio_slots").TCPDUMP_B_KIB and T.TOP_STATE == "10"
    txt = "tcpdump: listening on lvi_yt1\n1200 packets captured\n1210 packets received by filter\n0 packets dropped by kernel\n"
    assert T.tcpdump_counts(txt) == {"captured": 1200, "received": 1210, "kernel_drops": 0}
    assert T.tcpdump_counts("") == {"captured": None, "received": None, "kernel_drops": None}
    saved = T.SESSIONS
    T.SESSIONS = {"yt1": 0x11, "tw1": 0x12}
    try:
        smp = [{"t": float(i), "classes": {"lvi_yt1": {"1:10": {"backlog": 3000}}, "lvi_tw1": {"1:10": {"backlog": 0}}}}
               for i in range(60)]
        ph = {"state": "10", "buffer_kib": 65536, "seconds": 60.0, "samples": smp,
              "counts": {d: {"captured": 9, "received": 9, "kernel_drops": 0}
                         for d in ("lvi_yt1", "lvo_yt1", "lvi_tw1", "lvo_tw1")}}
        r = T.read_top_capture(ph)
        assert r["status"] == "pass" and r["pass"] and r["shaper_bound_share"] == {"yt1": 1.0, "tw1": 0.0}, r
        r = T.read_top_capture({**ph, "seconds": 59.9})
        assert r["status"] == "insufficient" and r["pass"] is False and not r["support"]["sufficient"], r
        bad = {**ph, "counts": {**ph["counts"], "lvi_tw1": {"captured": 9, "received": 12, "kernel_drops": 3}}}
        assert T.read_top_capture(bad)["status"] == "fail" and T.read_top_capture({**bad, "seconds": 10.0})["status"] == "fail"
        unread = {**ph, "counts": {**ph["counts"], "lvo_tw1": T.tcpdump_counts("")}}
        assert T.read_top_capture(unread)["status"] == "fail"               # an unread count is never a pass
    finally:
        T.SESSIONS = saved


def test_fast_reading_carries_the_change_minimum():
    fake = types.SimpleNamespace(
        change_drops=lambda ch: {"gate": "pass", "changes_read": len(ch)},
        ratio_non_straddling=lambda *a: {"gate": "pass", "qualifying_s": 40},
        whole_drop_share=lambda ch: {"drop_share": 0.0, "set_loss_mean": 0.0}, NOTES="", RATIO_LO=0.9, RATIO_HI=1.15)
    saved = T._fast
    T._fast = lambda: fake
    try:
        def ph(n):
            ch = [{"initial": True}] + [{"params": {}} for _ in range(n)]
            return {"plan": {"yt1": [{"matrix_id": "A@fast"}]}, "changes": {"yt1": ch}, "samples": [], "seconds": 150.0,
                    "seed": 0}
        r = T.read_fast_segment(ph(30))
        assert r["status"] == "pass" and r["pass"] and r["per_slot"]["yt1"]["changes"] == 30, r
        r = T.read_fast_segment(ph(29))
        assert r["status"] == "insufficient" and r["pass"] is False, r
        fake.change_drops = lambda ch: {"gate": "fail"}
        assert T.read_fast_segment(ph(29))["status"] == "fail"
    finally:
        T._fast = saved


def test_bulk_support_in_summarise():
    a = types.SimpleNamespace(iface="wlp0s20f3")
    saved = T.SESSIONS
    T.SESSIONS = {"tw1": 0x11}
    try:
        for state_s, want in ((40.0, "sufficient"), (33.0, "insufficient")):
            n = int(3 * state_s) + 1
            smp, b = [], 0
            for i in range(n):
                smp.append({"t": float(i), "classes": {"lvi_tw1": {"1:10": {"bytes": b, "backlog": 9000, "tokens": -5,
                                                                            "drops": 0}}}, "netem": {}, "links": {}})
                b += 200_000
            a.state_s = state_s
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "ping_tw1.log").write_text("")
                res = T.summarise(smp, [], {"tw1": ["5", "6", "7"]}, a, Path(d))
            sup = res["bulk_ratio_support"]
            assert sup["status"] == want, (state_s, sup)
            assert res["tw1"]["bulk_support"]["support"]["states"] == 3
            if want == "insufficient":
                assert res["rate_stage_validation"]["status"] in ("insufficient", "fail"), res["rate_stage_validation"]
    finally:
        T.SESSIONS = saved


def test_player_support_three_states():
    sys.path.insert(0, str(HERE))
    import test_player_load as P
    saved, saved_sh = T.SESSIONS, T.sh
    T.SESSIONS = {"tw1": 0x11}
    try:
        with tempfile.TemporaryDirectory() as d:
            tl = []
            for k, proto in enumerate(("h3", "tcp", "h3")):
                log = Path(d) / f"player_tw1_{k}.jsonl"
                recs = [{"t0": 1000.0 + k * 60 + 2 * i, "t1": 1000.5 + k * 60 + 2 * i, "bytes": 157500, "ok": True,
                         "alpn": proto if proto == "h3" else "http/1.1",
                         "five_tuple": {"transport": "udp" if proto == "h3" else "tcp", "local": "192.168.4.50:50001",
                                        "remote": "104.16.1.1:443"}} for i in range(10)]
                log.write_text("".join(json.dumps(r) + "\n" for r in recs))
                tl.append({"k": k, "session": "tw1", "state": "5", "proto": proto, "rate_kbit": 1600.0, "delay_ms": 40.0,
                           "loss_pct": 0.1, "log": str(log)})
            ph = {"t0_epoch": 1000.0, "samples": P._samples([20000] * 181), "kernel_drops": {"lvi_tw1": "0"},
                  "timeline": tl, "captures": {"lvi_tw1": "a.pcap"}}
            T.sh = lambda cmd, check=True, capture=False, print_only=False: ""
            r = T.read_player_phase(ph, 60.0)
            assert r["status"]["ratio"] == "pass", r["status"]                         # 3 x 60 s, each >= 10 s
            assert r["support"]["tw1"]["h3_exercised"]["support"]["h3_segments"] == 20
            assert r["status"]["h3_exercised"] == "pass" and r["status"]["no_leak"] == "pass", r["status"]
            assert r["pass"], r["status"]
            r = T.read_player_phase(ph, 59.0)                                           # a 59 s state: below 60
            # levi: option A's ratio is report-only; the phase gates on h3-exercised and no-leak
            assert r["status"]["ratio"] == "insufficient" and r["pass"], r["status"]
            assert r["gating"] == ["h3_exercised", "no_leak", "zero_capture_drops"] and "ratio" in r["reported"]
            for k in (0, 2):                                                           # 18 h3 segments: below 20
                p = Path(tl[k]["log"])
                p.write_text("".join(p.read_text().splitlines(keepends=True)[:9]))
            r = T.read_player_phase(ph, 60.0)
            assert r["status"]["h3_exercised"] == "insufficient" and not r["pass"], r["status"]
    finally:
        T.SESSIONS, T.sh = saved, saved_sh


def test_gates_summary():
    rep = {"goodput_gate": {"status": "insufficient", "support": M.goodput(20.0)},
           "top_capture": {"status": "pass", "support": M.capture_drops(66.0)},
           "results": {"bulk_ratio_support": {"status": "sufficient", "per_slot": {}},
                       "bulk_ratio_gate": {"status": "fail", "rule": "C5", "per_slot_state": {}},
                       "rate_stage_validation": {"status": "pass"}},
           "fast_segment": {"status": "insufficient", "per_slot": {"yt1": {"support": M.fast(12)}}},
           "walk_segment": {"status": "pass", "per_slot": {"yt1": {"support": M.walk(130.0, 60, 40.0)}}}}
    g = T.gates_summary(rep)
    assert g["goodput"]["status"] == "insufficient" and g["capture_drops"]["status"] == "pass"
    assert g["bulk_ratio"]["status"] == "fail" and g["fast"]["status"] == "insufficient" and "player_ratio" not in g
    assert g["walk"]["status"] == "pass" and g["walk"]["support"]["yt1"]["sufficient"]


def test_the_bulk_ratio_gates():
    """levi (C5, Q am. 16): each slot-state's span-corrected ratio in [0.9, 1.15] is the shaping gate; a ratio
    outside fails at any support, a missing ratio or a slot below its support reads insufficient."""
    ok_sup, low_sup = M.bulk_ratio([35.0] * 3), M.bulk_ratio([35.0, 35.0, 20.0])
    row = lambda st, r: {"state": st, "ratio_to_set": r}
    g = T.bulk_ratio_gate({"yt1": [row("3", 0.95), row("7", 1.0), row("10", 1.15)],
                           "tw1": [row("1", 0.9), row("2", 0.99), row("9", 1.02)]}, {"yt1": ok_sup, "tw1": ok_sup})
    assert g["status"] == "pass" and (T.BULK_RATIO_LO, T.BULK_RATIO_HI) == (0.9, 1.15), g
    g = T.bulk_ratio_gate({"yt1": [row("3", 0.89), row("7", 1.0), row("10", 1.0)]}, {"yt1": low_sup})
    assert g["status"] == "fail" and g["per_slot_state"]["yt1"][0]["gate"] == "fail"       # a fail stays a fail
    g = T.bulk_ratio_gate({"yt1": [row("3", 0.95), row("7", 1.0), row("10", 1.0)]}, {"yt1": low_sup})
    assert g["status"] == "insufficient"                                                   # below the support
    g = T.bulk_ratio_gate({"yt1": [row("3", None), row("7", 1.0), row("10", 1.0)]}, {"yt1": ok_sup})
    assert g["status"] == "insufficient"
    g = T.bulk_ratio_gate({"yt1": [row("3", 1.16), row("7", 1.0), row("10", 1.0)]}, {"yt1": ok_sup})
    assert g["status"] == "fail"


def test_decode_stress_short():
    a = types.SimpleNamespace(state="10", yt_fullscreen=False, dpms_off=False, layout="composite", iface="wlp0s20f3",
                              python="/v/python", minutes=7)
    _, cmd = D.commands(a, {"youtube": 1}, ["yt1"], Path("/o"))
    assert cmd[cmd.index("--episodes") + 1] == "7" and "--trio-stress" in cmd
    a.minutes = None
    assert "--episodes" not in D.commands(a, {"youtube": 1}, ["yt1"], Path("/o"))[1]
    box = {"swap_growth_kb": 0, "oom_kills": 0, "cpu_pct": {"p95": 70.0}, "gpu_above_report_line": []}

    def r(tag, top, t0=0.0, t1=420.0, shaped=20.0):
        return {"slot": tag, "duration_s": t1 - t0, "device_stall_readable": True, "device_stall_share": 0.0,
                "started_at": t0, "ended_at": t1, "seconds_at_top_tier": top, "top_state_spans": [[t0 + shaped, t1]]}
    # levi (prereg 40843f81): the support is the seconds all slots sit at the top state together, at any tier; a slot
    # holding its top tier for only part of them (tw1 at 1080 for 60 s) no longer makes it insufficient
    g = D.gate([r("yt1", 300.0), r("tw1", 60.0)], box, ["yt1", "tw1"], min_session_s=None)
    assert g["status"] == "pass" and g["pass"] and not g["fails"], g              # short sessions: no 10 min rule
    assert g["support"]["support"] == {"all_top_state_s": 400.0} and g["window"] == [[20.0, 420.0]], g
    g = D.gate([r("yt1", 300.0), r("tw1", 300.0, shaped=121.0)], box, ["yt1", "tw1"], min_session_s=None)
    assert g["status"] == "insufficient" and g["pass"] is False and not g["fails"], g   # tw1 shaped late: 299 s
    g = D.gate([r("yt1", 300.0), r("tw1", 300.0, t0=150.0, t1=570.0)], box, ["yt1", "tw1"], min_session_s=None)
    assert g["status"] == "insufficient" and g["support"]["support"]["all_top_state_s"] == 250.0, g   # not all at once
    no_top = dict(r("tw1", 300.0), top_state_spans=[])
    assert D.gate([r("yt1", 300.0), no_top], box, ["yt1", "tw1"], min_session_s=None)["status"] == "insufficient"
    assert D.gate([r("yt1", 300.0)], box, ["yt1", "tw1"], min_session_s=None)["status"] == "fail"   # tw1: no session
    g = D.gate([r("yt1", 300.0), r("tw1", 300.0)], dict(box, oom_kills=1), ["yt1", "tw1"], min_session_s=None)
    assert g["status"] == "fail", g
    assert D.gate([r("yt1", 300.0)], box, ["yt1"])["status"] == "fail"           # the full form keeps the 10 min rule
    assert D.overlap_s([r("yt1", 1.0, 0.0, 400.0), r("tw1", 1.0, 100.0, 300.0)]) == 200.0 and D.overlap_s([]) is None


def test_orchestrate_stress_episodes():
    """decode_stress --minutes: orchestrate --trio-stress --episodes M gives a held session M base episodes; a session
    that is not held (no pinned state) keeps its count, and so does a held one without the flag."""
    import orchestrate as O
    import test_trio_slots as TT
    for eps, want in ((None, str(O.EPISODES_V2["twitch"])), (7, "7")):
        with tempfile.TemporaryDirectory() as d:
            spawned = []

            def spawn(cmd, env, log):
                spawned.append(cmd)
                return TT.FakeProc(1, 0, [])
            saved = dict(O.TRIO_PINS), O.TRIO_STRESS, O.TRIO_STRESS_EPISODES
            O.TRIO_PINS.clear()
            O.TRIO_PINS.update({"tw1": {"state": "10", "reason": "decode_stress"}})
            O.TRIO_STRESS, O.TRIO_STRESS_EPISODES = True, eps
            try:
                TT._trio(Path(d), {"twitch": 5}, spawn)
            finally:
                O.TRIO_PINS.clear()
                O.TRIO_PINS.update(saved[0])
                O.TRIO_STRESS, O.TRIO_STRESS_EPISODES = saved[1], saved[2]
            c = spawned[0]
            assert c[c.index("--episodes") + 1] == want and "--hold-state" in c, (eps, c)


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_dry_short: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
