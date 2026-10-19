#!/usr/bin/env python3
"""ops/diag/player_load.py (Q am. 16; peter's spec, levi's read 2026-10-20): the live-edge player's join (B behind the
edge, caught up back to back), its pauses at the edge (one segment per D), stalls when a fetch outlasts the buffer, the
segment size from the platform's ladder (highest rung <= 0.8 x the set rate, x D; the ladders equal coverage_report's),
tt's reconnects, and every segment's record (bytes, buffer, behind, proto / alpn / five_tuple). No network: a fake
clock and fetch.

    python3 test_player_load.py        (exit 0 = pass)
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("player_load", HERE.parent / "ops" / "diag" / "player_load.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


def _player(fetch_s, D=2.0, B=3.0, seconds=30.0, **kw):
    c, log = Clock(), []

    def fetch(n):
        c.t += fetch_s(len(log))
        return n, {"proto": "tcp", "alpn": "http/1.1", "five_tuple": {"transport": "tcp", "local": "a:1", "remote": "b:2"},
                   "fallback": False}
    p = P.Player(fetch, 1000, D, B, clock=c.now, sleep=c.sleep, log=log.append, **kw)
    return p.run(seconds), log, c


def test_segment_size_from_the_ladder():
    assert P.rung_kbit("twitch", 1600) == 630.0                     # 0.8 x 1600 = 1280: highest rung below it
    assert P.rung_kbit("twitch", 100) == 230.0                      # below every rung: the lowest
    assert P.rung_kbit("youtube", 7500) == 3040.0
    assert P.segment_bytes("twitch", 1600) == int(630.0 * 1000 / 8 * 2.0)
    assert P.segment_bytes("tiktok", 2000) == int(1000.0 * 1000 / 8 * 1.0)
    import coverage_report as C
    assert {p: tuple(sorted(v[0] for v in C.RUNGS[p].values())) for p in P.LADDERS} == \
        {p: tuple(sorted(v)) for p, v in P.LADDERS.items()}
    assert P.CADENCE == {"twitch": {"D": 2.0, "B": 3.0}, "youtube": {"D": 5.0, "B": 13.0}, "tiktok": {"D": 1.0, "B": 3.0}}


def test_join_catch_up_then_one_segment_per_D():
    s, log, _ = _player(lambda i: 0.2, D=2.0, B=3.0, seconds=30.0)
    t = [r["t0"] for r in log]
    gaps = [b - a for a, b in zip(t, t[1:])]
    assert all(g < 0.5 for g in gaps[:1]), gaps[:3]                  # the join: back to back while behind
    steady = gaps[4:]
    assert steady and all(abs(g - 2.0) < 0.3 for g in steady), steady   # at the edge: one segment per D
    assert s["stalls"] == 0 and s["join_segments"] == 2 and s["errors"] == 0
    assert all(set(r) >= {"bytes", "buffer_s", "behind", "proto", "alpn", "five_tuple"} for r in log)


def test_a_slow_fetch_stalls_and_falls_behind():
    s, log, _ = _player(lambda i: 6.0 if i == 4 else 0.2, D=2.0, B=3.0, seconds=30.0)
    assert s["stalls"] >= 1 and s["stall_s"] > 0, s
    assert max(r["behind"] for r in log) >= 2                        # behind the edge after the slow segment


def test_tt_reconnects_every_20_s():
    n = []
    s, _, _ = _player(lambda i: 0.1, D=1.0, B=3.0, seconds=65.0, reconnect=lambda: n.append(1), reconnect_s=20.0)
    assert len(n) == 3 and s["segments"] > 50, (n, s)


def test_errors_are_counted_not_fatal():
    c = Clock()
    calls = []

    def fetch(n):
        calls.append(1)
        c.t += 0.1
        if len(calls) <= 2:
            raise OSError("down")
        return n, {"proto": "h3", "alpn": "h3", "five_tuple": None, "fallback": False}
    log = []
    s = P.Player(fetch, 500, 2.0, 3.0, clock=c.now, sleep=c.sleep, log=log.append).run(20.0)
    assert s["errors"] == 2 and s["segments"] > 0 and not log[0]["ok"] and log[0]["error"].startswith("OSError")


def _reading():
    sp = importlib.util.spec_from_file_location("player_reading", HERE.parent / "ops" / "diag" / "player_reading.py")
    R = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(R)
    return R


def _samples(backlogs, rate_bps_bytes=200_000, dev="lvi_tw1"):
    out, b = [], 0
    for i, bl in enumerate(backlogs):
        out.append({"t": float(i), "classes": {dev: {"1:10": {"bytes": b, "backlog": bl}}}})
        b += rate_bps_bytes
    return out


def test_qualifying_seconds_exclude_the_delay_line():
    """levi change 1: a second qualifies only if the backlog exceeds the delay line (rate x delay) + 2 MTU at both
    bounding samples; drops are no signal; < 10 qualifying seconds: insufficient."""
    R = _reading()
    rate, delay = 1600.0, 40.0                               # delay line 1600 kbit x 40 ms = 8000 B; threshold 11000 B
    assert R.delay_line_bytes(rate, delay) == 8000.0
    smp = _samples([9000] * 30)                              # only the delay line's content: never bound
    q, thr = R.qualifying(smp, "lvi_tw1", "1:10", 0, 30, rate, delay)
    assert thr == 11000 and q == [] and R.ratio_gate(q, rate)["gate"] == "insufficient"
    smp = _samples([20000] * 30, rate_bps_bytes=200_000)   # 200 kB/s = 1600 kbit/s: ratio 1.0
    q, _ = R.qualifying(smp, "lvi_tw1", "1:10", 0, 30, rate, delay)
    g = R.ratio_gate(q, rate)
    assert g == {"qualifying_s": 29, "ratio": 1.0, "gate": "pass"}, g
    assert R.ratio_gate(R.qualifying(_samples([20000] * 30, 150_000), "lvi_tw1", "1:10", 0, 30, rate, delay)[0],
                        rate)["gate"] == "fail"             # 1200 kbit/s: 0.75
    mixed = _samples([20000, 5000] * 15)                     # alternating: no second bound at both ends
    assert R.qualifying(mixed, "lvi_tw1", "1:10", 0, 30, rate, delay)[0] == []


def test_h3_share_tuples_leaks_bursts():
    R = _reading()
    segs = [{"t0": 10.0, "t1": 10.5, "bytes": 1000, "ok": True, "alpn": "h3",
             "five_tuple": {"transport": "udp", "local": "192.168.4.50:50001", "remote": "104.16.1.1:443"}}] * 9 + \
        [{"t0": 20.0, "t1": 20.5, "bytes": 0, "ok": False, "alpn": None, "five_tuple": None}]
    assert abs(R.h3_share(segs) - 0.9) < 1e-9
    assert R.five_tuples(segs) == {("udp", "104.16.1.1", 443, 50001)}
    txt = ("12:00:00.000000 IP 104.16.1.1.443 > 192.168.4.50.50001: UDP, length 1200\n"
           "12:00:00.100000 IP 1.1.1.1.53 > 192.168.4.50.40000: UDP, length 80\n"
           "12:00:00.200000 IP 104.16.1.1.443 > 192.168.4.50.50001: tcp 1448\n")
    pk = R.parse_tcpdump(txt)
    assert pk[0] == ("udp", "104.16.1.1", 443, "192.168.4.50", 50001) and pk[2][0] == "tcp"
    lk = R.leaks({"tw1": {("udp", "104.16.1.1", 443, 50001)}}, {"lvi_tw1": pk, "lvi_def": pk[:2], "lvi_yt1": []})
    assert lk == {"tw1": {"lvi_def": 1, "lvi_yt1": 0}}, lk     # its own capture is never counted
    smp = _samples([0] * 40, rate_bps_bytes=300_000)
    b = R.bursts(smp, "lvi_tw1", "1:10", [{"t0": 1000.0, "t1": 1001.0}, {"t0": 1005.0, "t1": 1006.0}], 1600.0, 990.0)
    assert len(b) == 1 and b[0] == 600_000 - 400_000, b      # 2 s at 300 kB/s vs the set 200 kB/s


def test_the_dry_run_phase_reading_and_command(tmp_path=None):
    """The player phase in trio_dry_run: h3 / TCP alternating by state, the player's command in the slot's cgroup,
    and read_player_phase's gates (ratio, h3 exercised, no leak, zero drops) on a synthetic phase."""
    import json
    import tempfile
    import types
    sp = importlib.util.spec_from_file_location("trio_dry_run", HERE.parent / "ops" / "diag" / "trio_dry_run.py")
    T = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(T)
    assert [T.player_proto(k) for k in range(4)] == ["h3", "tcp", "h3", "tcp"]
    a = types.SimpleNamespace(player_python="/v/py", player_url="https://speed.cloudflare.com/__down",
                              player_fallback_url="http://m/x.iso", state_s=60.0)
    d = Path(tmp_path or tempfile.mkdtemp())
    cmd = T.player_cmd(a, "tw1", 1, {"rate_kbit": 1600}, d / "p.jsonl")
    assert "--platform twitch" in cmd and "--proto tcp" in cmd and "--rate-kbit 1600" in cmd and "--seconds 59.0" in cmd
    saved = T.SESSIONS
    T.SESSIONS = {"tw1": 0x11}
    try:
        log = d / "player_tw1_0.jsonl"
        recs = [{"t0": 1000.0 + 2 * i, "t1": 1000.5 + 2 * i, "bytes": 157500, "ok": True, "alpn": "h3", "buffer_s": 3.0,
                 "behind": 0, "five_tuple": {"transport": "udp", "local": "192.168.4.50:50001", "remote": "104.16.1.1:443"}}
                for i in range(25)] + [{"summary": {"segments": 25, "errors": 0, "stalls": 0, "stall_s": 0.0}}]
        log.write_text("".join(json.dumps(r) + "\n" for r in recs))
        ph = {"t0_epoch": 1000.0, "samples": _samples([20000] * 60), "kernel_drops": {"lvi_tw1": "0", "lvi_def": "0"},
              "timeline": [{"k": 0, "session": "tw1", "state": "5", "proto": "h3", "rate_kbit": 1600.0, "delay_ms": 40.0,
                            "loss_pct": 0.1, "log": str(log)}],
              "captures": {"lvi_tw1": "a.pcap", "lvi_def": "b.pcap"}}
        T.sh = lambda cmd, check=True, capture=False, print_only=False: (
            "12:00:00.0 IP 104.16.1.1.443 > 192.168.4.50.50001: UDP, length 1200\n" if "a.pcap" in cmd else "")
        r = T.read_player_phase(ph, 60.0)
        st = r["per_slot"]["tw1"][0]
        assert st["gate"] == "pass" and st["h3"]["gate"] == "pass" and r["gated"]["no_leak"] and r["pass"], r["gated"]
        T.sh = lambda cmd, check=True, capture=False, print_only=False: (
            "12:00:00.0 IP 104.16.1.1.443 > 192.168.4.50.50001: UDP, length 1200\n")   # also on lvi_def: a leak
        assert T.read_player_phase(ph, 60.0)["gated"]["no_leak"] is False
    finally:
        T.SESSIONS = saved


def main() -> int:
    test_qualifying_seconds_exclude_the_delay_line()
    test_h3_share_tuples_leaks_bursts()
    test_the_dry_run_phase_reading_and_command()
    test_segment_size_from_the_ladder()
    test_join_catch_up_then_one_segment_per_D()
    test_a_slow_fetch_stalls_and_falls_behind()
    test_tt_reconnects_every_20_s()
    test_errors_are_counted_not_fatal()
    print("test_player_load: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
