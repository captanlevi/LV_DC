#!/usr/bin/env python3
"""ops/diag/player_load.py (Q am. 16; peter's spec, levi's read 2026-10-20): the live-edge player's join (B behind the
edge, caught up back to back), its pauses at the edge (one segment per D), stalls when a fetch outlasts the buffer, the
segment size from the platform's ladder (highest rung <= 0.8 x the set rate, x D; the ladders equal coverage_report's),
tt's reconnects, and every segment's record (bytes, buffer, behind, proto / alpn / five_tuple). No network: a fake
clock and fetch.

    python3 test_player_load.py        (exit 0 = pass)
"""
import importlib.util
import json
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


def _samples(backlogs, rate_bps_bytes=200_000, dev="lvi_tw1", tokens=-5):
    out, b = [], 0
    for i, bl in enumerate(backlogs):
        out.append({"t": float(i), "classes": {dev: {"1:10": {"bytes": b, "backlog": bl, "tokens": tokens}}}})
        b += rate_bps_bytes
    return out


def _fast(seconds, busy_of_10=10, rate_bytes=200_000, dev="lvi_tw1", t0=0.0, flat=()):
    """10 Hz token samples: in each second the first busy_of_10 samples have tokens <= 0 and bytes moving; seconds in
    `flat` are a netem-held burst (tokens refilling, no bytes)."""
    out, b = [], 0
    for k in range(int(seconds * 10)):
        t = t0 + k / 10
        sec = int(k // 10)
        busy = sec not in flat and (k % 10) < busy_of_10
        if busy:
            b += rate_bytes // 10
        out.append({"t": round(t, 3), dev: {"bytes": b, "tokens": -3 if busy else 4000}})
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


def test_option_a_the_rate_stage_busy():
    """levi's ruling (00132, option A; its 100 ms form set after 730b0a0a): a sampling interval qualifies when tokens <= 0
    at both its samples and bytes moved; the ratio is over the qualifying intervals, so a load busy ~70 % of each second
    (tt's 1 s chunks) reads its busy intervals' rate, not ~0.85; a netem-held burst (tokens refilling, nothing sent)
    never qualifies; at 1 Hz (weaker) tokens <= 0 at both bounding samples; validated on the bulk: >= 0.9 of the time
    and the ratio within 0.02 of the bulk's own."""
    R = _reading()
    q, n = R.rate_stage_intervals_10hz(_fast(30), "lvi_tw1", 0.0, 30.0)
    assert len(q) == 299 and abs(n - 29.9) < 1e-6, (len(q), n)        # every interval busy
    g = R.rate_stage_gate(_fast(30), [], "lvi_tw1", 0.0, 30.0, 1600.0)
    assert g["ratio"] == 1.0 and g["gate"] == "pass" and g["mode"].startswith("10 Hz intervals") and g["qualifying_share"] == 1.0, g
    g8 = R.rate_stage_gate(_fast(30, busy_of_10=8), [], "lvi_tw1", 0.0, 30.0, 1600.0)   # busy 80 % of each second
    assert g8["ratio"] == 1.0 and 0.65 <= g8["qualifying_share"] <= 0.75 and g8["gate"] == "pass", g8
    tail = _fast(30, busy_of_10=7)
    for x in tail:                                                     # the deficit outlasts the sending: no bytes
        if round(x["t"] * 10) % 10 == 7:
            x["lvi_tw1"]["tokens"] = -1
    gt = R.rate_stage_gate(tail, [], "lvi_tw1", 0.0, 30.0, 1600.0)
    assert gt["ratio"] == 1.0, gt                                      # the per-second form read these at ~0.85
    q, _ = R.rate_stage_intervals_10hz(_fast(30, flat=set(range(10, 20))), "lvi_tw1", 0.0, 30.0)
    assert abs(sum(d for d, _ in q) - 19.8) < 0.15, sum(d for d, _ in q)  # the 10 held seconds (and their edges) out
    g1 = R.rate_stage_gate(None, _samples([0] * 30), "lvi_tw1", 0.0, 29.0, 1600.0)
    assert g1["mode"].startswith("1 Hz (weaker)") and g1["gate"] == "pass" and g1["ratio"] == 1.0, g1
    assert R.rate_stage_gate(None, _samples([0] * 30, tokens=900), "lvi_tw1", 0.0, 29.0, 1600.0)["gate"] == "insufficient"
    assert R.validate_on_bulk({"qualifying_share": 0.968, "ratio": 0.984}, 0.964)["ok"]   # exactly 0.02: within
    ok = R.validate_on_bulk({"qualifying_share": 0.95, "ratio": 0.99}, 0.995)
    assert ok["ok"] and not R.validate_on_bulk({"qualifying_share": 0.95, "ratio": 0.96}, 0.99)["ok"]
    assert not R.validate_on_bulk({"qualifying_share": 0.8, "ratio": 0.99}, 0.99)["ok"]
    # option B, reported: (size / rate + RTT) / actual for fetches >= 4 x BDP
    segs = [{"ok": True, "bytes": 400_000, "t0": 0.0, "t1": 2.1}, {"ok": True, "bytes": 1000, "t0": 0.0, "t1": 1.0}]
    b = R.fetch_delivery(segs, 1600.0, 0.1)                           # 2.0 s at the rate + 0.1 RTT = 2.1: 1.0
    assert b["n"] == 1 and b["p50"] == 1.0 and b["share_in_band"] == 1.0, b
    # the sampler: TOKENS_HZ rows of bytes and tokens per device, on its own thread
    sp = importlib.util.spec_from_file_location("trio_dry_run", HERE.parent / "ops" / "diag" / "trio_dry_run.py")
    T = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(T)
    import time as _t
    ts = T.TokenSampler(["lvi_tw1"], _t.time(), hz=100, read=lambda d: {"bytes": 7, "tokens": -1}).start()
    _t.sleep(0.15)
    rows = ts.stop()
    assert rows and rows[0]["lvi_tw1"] == {"bytes": 7, "tokens": -1} and "t" in rows[0], rows[:2]
    txt = ("class htb 1:10 root leaf 10: prio 0 rate 1600Kbit ceil 1600Kbit burst 1600b cburst 1600b \n"
           " Sent 1234 bytes 10 pkt (dropped 0, overlimits 3 requeues 0) \n backlog 3000b 2p requeues 0\n"
           " lended: 10 borrowed: 0 giants: 0\n tokens: -2500 ctokens: -2500\n")
    assert T.parse_class_stats(txt)["1:10"]["tokens"] == -2500


def test_the_bulk_validates_option_a_and_the_rerun_mode():
    """summarise reads option A on every bulk slot-state beside the bulk's own ratio (levi: >= 0.9 of the seconds,
    within 0.02), reported as results.rate_stage_validation; --player-rerun runs the tree, the bulk and the player phase
    only."""
    import tempfile
    import types
    sp = importlib.util.spec_from_file_location("trio_dry_run", HERE.parent / "ops" / "diag" / "trio_dry_run.py")
    T = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(T)
    saved = T.SESSIONS
    try:
        T.SESSIONS = {"tw1": 0x11}
        p = T.state_params("5")
        per_s = int(p["rate_kbit"] * 1000 / 8)
        d = Path(tempfile.mkdtemp())
        (d / "ping_tw1.log").write_text("")
        samples = [{"t": float(i), "classes": {"lvi_tw1": {"1:10": {"bytes": i * per_s, "backlog": 20000, "tokens": -5}}},
                    "netem": {}, "ingress_by_mark": {}, "links": {}} for i in range(65)]
        a = types.SimpleNamespace(state_s=60.0, iface="wlp0s20f3")
        res = T.summarise(samples, [], {"tw1": ["5"]}, a, d, fast=_fast(65, rate_bytes=per_s))
        v = res["tw1"]["per_state"][0]["rate_stage_validation"]
        assert res["rate_stage_validation"]["ok"] and v["qualifying_share"] >= 0.9 and abs(v["ratio_a"] - v["bulk_ratio"]) <= 0.02, v
        held = _fast(65, rate_bytes=per_s, flat=set(range(10, 40)))       # the rate stage idle half the window
        assert not T.summarise(samples, [], {"tw1": ["5"]}, a, d, fast=held)["rate_stage_validation"]["ok"]
    finally:
        T.SESSIONS = saved
    src = (HERE.parent / "ops" / "diag" / "trio_dry_run.py").read_text()
    for skipped in ("if not rerun:", "if not a.no_single_reference and not rerun:", "and not NO_IFB and not rerun:",
                    "if a.player_load or rerun:"):
        assert skipped in src, skipped


def test_h3_failure_causes_and_the_probe():
    """levi on the 5ba0b94 dry run (0 h3 segments, every attempt an error): speed.cloudflare.com serves no HTTP/3 (its
    QUIC handshake ends in TLS alert 40); the endpoint is h3.speed.cloudflare.com; every failed attempt carries its
    cause; --probe fails the dry run before any phase; IPv4-mapped peers are logged as IPv4 (the leak check's form)."""
    import io
    import contextlib
    assert P.DEFAULT_URL == "https://h3.speed.cloudflare.com/__down"
    assert P.close_cause(0x128) == "quic close 0x128 (TLS alert 40 handshake_failure)"
    assert P.close_cause(0x0, "bye", 0x1c) == "quic close 0x0 frame 0x1c reason 'bye'" and "no CONNECTION_CLOSE" in P.close_cause(None)
    assert P.norm_addr("::ffff:104.18.8.38", 443) == "104.18.8.38:443" and P.norm_addr("::", 5) == ":::5"
    R = _reading()
    seg = {"five_tuple": {"transport": "udp", "local": ":::50001", "remote": "::ffff:104.16.1.1:443"}}
    assert R.five_tuples([seg]) == {("udp", "104.16.1.1", 443, 50001)}           # older logs normalised in the reader
    saved = P.h3_fetcher
    try:
        def bad(url):
            def fetch(n):
                raise P.H3Error("handshake to speed.cloudflare.com: quic close 0x128 (TLS alert 40 handshake_failure)")
            return fetch, lambda: None, lambda: None
        P.h3_fetcher = bad
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = P.main(["--platform", "twitch", "--rate-kbit", "1000", "--proto", "h3", "--seconds", "1", "--probe"])
        rec = json.loads(out.getvalue())
        assert rc == 5 and rec["ok"] is False and "TLS alert 40" in rec["error"], rec
        P.h3_fetcher = lambda url: ((lambda n: (n, {"proto": "h3", "alpn": "h3", "five_tuple": None, "fallback": False})),
                                    lambda: None, lambda: None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = P.main(["--platform", "twitch", "--rate-kbit", "1000", "--proto", "h3", "--seconds", "1", "--probe"])
        assert rc == 0 and json.loads(out.getvalue())["alpn"] == "h3"
    finally:
        P.h3_fetcher = saved
    src = (HERE.parent / "ops" / "diag" / "trio_dry_run.py").read_text()
    assert '"--probe", "--url", a.player_url' in src and 'default="https://h3.speed.cloudflare.com/__down"' in src


def main() -> int:
    test_qualifying_seconds_exclude_the_delay_line()
    test_h3_share_tuples_leaks_bursts()
    test_the_dry_run_phase_reading_and_command()
    test_segment_size_from_the_ladder()
    test_join_catch_up_then_one_segment_per_D()
    test_a_slow_fetch_stalls_and_falls_behind()
    test_tt_reconnects_every_20_s()
    test_errors_are_counted_not_fatal()
    test_h3_failure_causes_and_the_probe()
    test_option_a_the_rate_stage_busy()
    test_the_bulk_validates_option_a_and_the_rerun_mode()
    print("test_player_load: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
