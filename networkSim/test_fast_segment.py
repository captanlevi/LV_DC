#!/usr/bin/env python3
"""The dry run's @fast segment (LVQoE prereg Addendum Q am. 17's landing condition; ops/diag/fast_segment.py and
trio_dry_run's phase): each slot's plan is its platform's @fast family from the collector's generator (seeded per
slot), the change-instant drop gate against the set loss, the ratio on backlogged seconds that straddle no change, and
the phase's schedule of changes, snapshots and samples. No root, no network: a fake clock, tc stubbed.

    python3 test_fast_segment.py        (exit 0 = pass)
"""
import importlib.util
import random
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name):
    sp = importlib.util.spec_from_file_location(name, HERE.parent / "ops" / "diag" / f"{name}.py")
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


F = _load("fast_segment")


def test_the_plan_is_the_platforms_fast_family_seeded_per_slot():
    state = random.getstate()
    a = F.schedule("twitch", "tw1", 120.0, seed=0)
    assert random.getstate() == state
    assert a == F.schedule("twitch", "tw1", 120.0, seed=0) and a != F.schedule("twitch", "tw2", 120.0, seed=0)
    assert {e["matrix_id"] for e in a} == {"TW-M@fast"}
    assert F.schedule("youtube", "yt1", 30.0)[0]["matrix_id"] == "A@fast"
    assert F.schedule("tiktok", "tt1", 30.0)[0]["matrix_id"] == "TT-M@fast"
    assert all(1.0 - 1e-9 <= e["dur"] <= 7.0 for e in a)            # 2.4-6 s re-draws, cut at 12 s episode ends
    assert a[0]["t"] == 0.0 and a[-1]["t"] < 120.0 <= a[-1]["t"] + a[-1]["dur"] + 1e-6
    assert all(b["t"] == round(x["t"] + x["dur"], 3) or abs(b["t"] - x["t"] - x["dur"]) < 2e-3 for x, b in zip(a, a[1:]))


def _snap(t, pk, dr):
    return {"t": t, "netem": {"packets": pk, "drops": dr}}


def test_change_drops_against_the_set_loss():
    p = {"loss_pct": 0.2, "rate_kbit": 1000, "delay_ms": 40}
    ch = [{"pre": _snap(5.0 * i, 10000 * i, 2 * i), "post": _snap(5.0 * i + 0.02, 10000 * i + 20, 2 * i),
           "settle": _snap(5.0 * i + 1.0, 10000 * i + 1000, 2 * i + 2), "params": p} for i in range(1, 21)]
    r = F.change_drops(ch)
    assert r["changes_read"] == 20 and r["drops"] == 40 and r["gate"] == "pass", r   # 2 per 1002 at 0.2 %: on the loss
    ch[7]["settle"] = _snap(ch[7]["settle"]["t"], 10000 * 8 + 1000, 2 * 8 + 60)     # a burst of drops at one change
    r = F.change_drops(ch)
    assert r["gate"] == "fail" and r["max_excess"] > 50, r
    ch[3]["settle"] = None                                         # a missing settle ends at the next change's pre
    assert F.change_drops(ch)["changes_read"] == 20
    assert F.change_drops([{"pre": {"t": -3.0}, "post": {"t": -3.0}, "settle": None, "params": p}])["gate"] == "not read"


def test_the_ratio_skips_seconds_with_a_change():
    p1 = {"rate_kbit": 1600, "delay_ms": 40, "loss_pct": 0.1}
    p2 = {"rate_kbit": 800, "delay_ms": 40, "loss_pct": 0.1}
    ch = [{"pre": {"t": -3.0}, "post": {"t": -3.0}, "params": p1},
          {"pre": {"t": 10.4}, "post": {"t": 10.45}, "params": p2}]
    samples, b = [], 0
    for k in range(25):
        samples.append({"t": float(k), "classes": {"lvi_tw1": {"1:10": {"bytes": b, "backlog": 20000}}}})
        b += 200_000 if k < 10 else 100_000                    # 1600 kbit/s, then 800 after the change in [10, 11]
    r = F.ratio_non_straddling(samples, "lvi_tw1", "1:10", ch, 0.0, 24.0)
    assert r == {"qualifying_s": 23, "ratio": 1.0, "gate": "pass"}, r             # 24 seconds less the straddling one
    assert F.ratio_non_straddling(samples, "lvi_tw1", "1:10", ch, 0.0, 9.0)["gate"] == "insufficient"
    low = [dict(x, classes={"lvi_tw1": {"1:10": {"bytes": x["classes"]["lvi_tw1"]["1:10"]["bytes"], "backlog": 6000}}})
           for x in samples]                     # inside both delay lines + 2 MTU (11000 B, then 7000 B): never bound
    assert F.ratio_non_straddling(low, "lvi_tw1", "1:10", ch, 0.0, 24.0)["qualifying_s"] == 0


def test_the_phase_applies_every_change_and_snapshots_it():
    T = _load("trio_dry_run")
    clock = types.SimpleNamespace(t=1000.0)
    saved = (T.SESSIONS, T.sh, T.start, T.stop, T.class_stats, T.netem_stats)
    cmds = []
    try:
        T.SESSIONS = {"tw1": 0x11, "yt1": 0x12}
        T.sh = lambda cmd, check=True, capture=False, print_only=False: cmds.append(cmd) or ""
        T.start = lambda cmd, log: None
        T.stop = lambda procs: None
        T.class_stats = lambda dev: {"1:10": {"bytes": int(clock.t * 1000), "backlog": 50000, "drops": 0}}
        T.netem_stats = lambda dev: {"1:10": {"packets": int(clock.t * 100), "drops": 0}}
        a = types.SimpleNamespace(fast_segment_s=30.0, fast_seed=0, iface="wlp0s20f3", url="http://x/y", bulk_streams=2)
        ph = T.fast_segment_phase(a, Path("/tmp"), clock=lambda: clock.t,
                                  sleep=lambda s: setattr(clock, "t", clock.t + max(0.0, s)))
        for s in ("tw1", "yt1"):
            ch = ph["changes"][s]
            assert ch[0].get("initial") and len(ch) - 1 == len(ph["plan"][s]) - 1
            assert all(c["pre"]["t"] <= c["post"]["t"] for c in ch[1:])
            assert all(c["settle"] is None or abs(c["settle"]["t"] - c["pre"]["t"] - F.SETTLE_S) < 1e-6 for c in ch[1:])
            assert sum(1 for x in cmds if f"lvi_{s}" in x and "htb rate" in x) == len(ph["plan"][s])
        assert len(ph["samples"]) == 31
        r = T.read_fast_segment(ph)
        assert set(r["per_slot"]) == {"tw1", "yt1"} and r["per_slot"]["tw1"]["matrix_id"] == "TW-M@fast", r
        assert r["per_slot"]["tw1"]["change_drops"]["gate"] == "pass", r
    finally:
        T.SESSIONS, T.sh, T.start, T.stop, T.class_stats, T.netem_stats = saved


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_fast_segment: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
