#!/usr/bin/env python3
"""v3 A18 (levi / supervisor 19:33-19:41): FastTcSampler reads the session's own shaped classes (its IFB ingress 1:10
and its egress class) every FAST_TC_S onto tc_fast.jsonl, compact rows {t, in, out} with FAST_TC_FIELDS lists (null when
unread); it keeps a fixed grid (a late tick skips, counted, never bunches), counts unread ticks, and its summary carries
the gap quantiles and the measured cost (thread CPU + reaped children over wall). exp.py starts it beside the 1 s
sampler and records meta concurrency.tc_fast.

    python3 test_tc_fast.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trio_slots as T  # noqa: E402


def _cls(cid, by, backlog):
    return (f"class htb {cid} root leaf 8001: prio 0 rate 5Mbit ceil 5Mbit burst 1600b cburst 1600b \n"
            f" Sent {by} bytes 10 pkt (dropped 1, overlimits 2 requeues 0) \n backlog {backlog}b 1p requeues 0\n")


def test_rows_grid_and_summary():
    yt = T.SLOTS["yt1"]
    n = {"i": 0}

    def read(dev):
        if dev == yt.ifb_in:
            n["i"] += 1
            return "" if n["i"] == 3 else _cls("1:10", 1000 * n["i"], 1514)
        if dev == T.IFACE_DEFAULT:
            return _cls(yt.egress_class, 77, 0)
        return ""
    with tempfile.TemporaryDirectory() as d:
        s = T.FastTcSampler(Path(d), yt, period_s=0.02, read=read).start()
        time.sleep(0.3)
        sm = s.stop()
        rows = [json.loads(l) for l in (Path(d) / "tc_fast.jsonl").read_text().splitlines()]
    assert sm["samples"] == len(rows) >= 5, (sm, len(rows))
    assert rows[0]["in"] == [1000, 10, 1, 2, 1514, 1] and rows[0]["out"] == [77, 10, 1, 2, 0, 1]
    assert rows[2]["in"] is None and sm["unread"] == 1                      # an unread tick: null, counted
    assert list(T.FAST_TC_FIELDS) == ["bytes", "packets", "drops", "overlimits", "backlog", "qlen"]
    for k in ("gap_p50_s", "gap_p99_s", "gap_max_s", "wall_s", "thread_cpu_s", "children_cpu_s", "cpu_share_of_core"):
        assert k in sm, k
    assert 0.0 < sm["gap_p50_s"] < 0.2


def test_late_ticks_skip():
    yt = T.SLOTS["yt1"]

    def slow(dev):
        time.sleep(0.05)                                    # a read slower than the period
        return _cls("1:10", 1, 0)
    with tempfile.TemporaryDirectory() as d:
        s = T.FastTcSampler(Path(d), yt, period_s=0.02, read=slow).start()
        time.sleep(0.4)
        sm = s.stop()
    assert sm["skipped_ticks"] > 0 and sm["samples"] > 0


def test_exp_wiring():
    src = (HERE / "exp.py").read_text()
    assert "tc_fast = trio_slots.FastTcSampler(session_dir, SLOT).start() if SLOT is not None else None" in src
    assert 'TRIO["tc_fast"] = tc_fast.stop() if tc_fast is not None else None' in src
    assert T.FAST_TC_S == 0.1


def main() -> int:
    test_rows_grid_and_summary()
    test_late_ticks_skip()
    test_exp_wiring()
    print("test_tc_fast: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
