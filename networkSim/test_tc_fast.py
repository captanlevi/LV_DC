#!/usr/bin/env python3
"""v3 A18 (levi / supervisor 19:33-19:41; brad / levi 21:27-21:28): FastTcSampler reads the session's own shaped classes
(its IFB ingress 1:10 and its egress class) every FAST_TC_S onto tc_fast.jsonl: the first line the wall anchor
{wall0, mono0}, then rows {t, in, t_in, out, t_out}: FAST_TC_FIELDS lists (null when unread) and each class's read
instants [start, end] on the monotonic clock (readers bin on those); a fixed grid (a late tick skips, counted); unread
counted per class; gaps from the monotonic clock; cost = thread CPU + each tc child's own CPU (os.wait4). exp.py starts
it beside the 1 s sampler and records meta concurrency.tc_fast.

    python3 test_tc_fast.py        (exit 0 = pass)
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trio_slots as T  # noqa: E402

YT = T.SLOTS["yt1"]


def _cls(cid, by, backlog):
    return (f"class htb {cid} root leaf 8001: prio 0 rate 5Mbit ceil 5Mbit burst 1600b cburst 1600b \n"
            f" Sent {by} bytes 10 pkt (dropped 1, overlimits 2 requeues 0) \n backlog {backlog}b 1p requeues 0\n")


def _rows(d):
    lines = [json.loads(l) for l in (Path(d) / "tc_fast.jsonl").read_text().splitlines()]
    assert set(lines[0]) == {"anchor"} and set(lines[0]["anchor"]) == {"wall0", "mono0"}
    return lines[0]["anchor"], lines[1:]


def test_rows_instants_cost_and_unread():
    n = {"in": 0, "out": 0}

    def read(dev):                                           # 10 ms per exec, 0.003 s of child CPU each
        time.sleep(0.01)
        if dev == YT.ifb_in:
            n["in"] += 1
            return (_cls("1:10", 1000 * n["in"], 1514), 0.003)
        n["out"] += 1
        return ("" if n["out"] == 2 else _cls(YT.egress_class, 77, 0), 0.003)    # an out-only failure
    with tempfile.TemporaryDirectory() as d:
        s = T.FastTcSampler(Path(d), YT, period_s=0.05, read=read).start()
        time.sleep(0.4)
        sm = s.stop()
        anchor, rows = _rows(d)
    assert sm["samples"] == len(rows) >= 4, (sm, len(rows))
    assert rows[0]["in"] == [1000, 10, 1, 2, 1514, 1] and rows[0]["out"] == [77, 10, 1, 2, 0, 1]
    for r in rows:                                           # each class's read bracketed, in before out, after t
        assert r["t"] <= r["t_in"][0] <= r["t_in"][1] <= r["t_out"][0] <= r["t_out"][1]
        assert r["t_in"][1] - r["t_in"][0] >= 0.009 and r["t_out"][1] - r["t_out"][0] >= 0.009
    assert rows[1]["out"] is None and rows[1]["in"] is not None
    assert sm["unread_out"] == 1 and sm["unread_in"] == 0
    assert abs(sm["tc_child_cpu_s"] - round(0.003 * (n["in"] + n["out"]), 2)) <= 0.011         # the wait4 sum
    assert sm["anchor"] == anchor and abs(anchor["wall0"] - time.time()) < 60
    assert 0.03 < sm["gap_p50_s"] < 0.1 and "cpu_share_of_core" in sm


def test_late_ticks_skip():
    def slow(dev):
        time.sleep(0.05)                                     # two reads slower than the period
        return _cls("1:10", 1, 0)
    with tempfile.TemporaryDirectory() as d:
        s = T.FastTcSampler(Path(d), YT, period_s=0.02, read=slow).start()
        time.sleep(0.4)
        sm = s.stop()
    assert sm["skipped_ticks"] > 0 and sm["samples"] > 0


def test_wait4_reader():
    """the real reader: exec (no shell), the child's own CPU from os.wait4, the output; a failing tc reads empty"""
    with tempfile.TemporaryDirectory() as d:
        tc = Path(d) / "tc"
        tc.write_text("#!/bin/sh\n[ \"$5\" = bad ] && exit 2\nprintf 'class htb 1:10 root rate 5Mbit\\n'\n")
        tc.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{d}:{old}"
        try:
            out, cpu = T._tc_classes("lvi_yt1")
            bad, _ = T._tc_classes("bad")
        finally:
            os.environ["PATH"] = old
    assert out.startswith("class htb 1:10") and cpu >= 0.0 and bad == ""


def test_exp_wiring():
    src = (HERE / "exp.py").read_text()
    assert "tc_fast = trio_slots.FastTcSampler(session_dir, SLOT).start() if SLOT is not None else None" in src
    assert 'TRIO["tc_fast"] = tc_fast.stop() if tc_fast is not None else None' in src
    assert T.FAST_TC_S == 0.1 and list(T.FAST_TC_FIELDS) == ["bytes", "packets", "drops", "overlimits", "backlog", "qlen"]


def main() -> int:
    test_rows_instants_cost_and_unread()
    test_late_ticks_skip()
    test_wait4_reader()
    test_exp_wiring()
    print("test_tc_fast: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
