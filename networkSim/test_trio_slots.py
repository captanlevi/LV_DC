#!/usr/bin/env python3
"""t53 --concurrent: trio_slots.py (the slot tree = gate 1's tree, each slot's teardown touches only its own pieces,
the socket-owner statement, tc samples, the sibling timeline, the A2 rootfs floor), flow_overlap.py (5-tuple / QUIC CID
uniqueness, sibling families, default-class media, DNS presence) and orchestrate.run_trio (rolling slots, stagger,
drain, preflight / spawn failures drain instead of tearing down under running siblings). No root, no network.

    python3 test_trio_slots.py        (exit 0 = pass)
"""
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orchestrate as O                                  # noqa: E402
assert "trio_slots" not in sys.modules, "single mode imports trio_slots"
import trio_slots as T                                   # noqa: E402
import flow_overlap as F                                 # noqa: E402

spec = importlib.util.spec_from_file_location("trio_dry_run", HERE.parent / "ops" / "diag" / "trio_dry_run.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)
IF = "wlp0s20f3"


def test_tree_is_gate1s():
    ours = T.shared_up_cmds(IF) + [c for s in T.SLOTS.values() for c in T.slot_up_cmds(s, IF)]
    for c in D.tree_up_cmds(IF):
        if c.startswith("modprobe") or " del " in c or c.startswith("mkdir"):
            continue
        if c.startswith("nft add rule"):
            assert any(o.startswith(c[:-1]) and 'comment "lvdc slot' in o for o in ours), c
        else:
            assert c in ours, c
    assert {s.mark for s in T.SLOTS.values()} == set(D.SESSIONS.values())
    assert {s.cdp_port for s in T.SLOTS.values()}.isdisjoint({9222})
    assert len({s.prio for s in T.SLOTS.values()}) == 3
    # nft resolves the cgroup path when the rule is added: the cgroup first
    up = T.slot_up_cmds(T.SLOTS["youtube"], IF)
    assert up[0] == "mkdir -p /sys/fs/cgroup/lvdc.slice/yt" and up[1].startswith("nft add rule")


def test_slot_down_touches_only_its_own():
    yt = T.SLOTS["youtube"]
    down = "\n".join(T.slot_down_cmds(yt, IF, nft_handle=7))
    for o in ("tw", "tt"):
        assert f"lvi_{o}" not in down and f"lvo_{o}" not in down and f"lvdc.slice/{o}" not in down
    assert "prio 11" not in down and "prio 12" not in down and "1:12" not in down and "1:13" not in down
    assert "cgroup.kill" in down.splitlines()[0]                         # leftovers killed before anything else
    assert "nft delete rule inet lvdc_trio out handle 7" in down
    assert "qdisc del dev wlp0s20f3 root" not in down and "ingress" not in down.replace("parent ffff:", "")
    assert "ifb0" not in down and "ifb1" not in down
    listing = ('table inet lvdc_trio {\n chain out { # handle 1\n  socket cgroupv2 level 2 "lvdc.slice/yt" meta mark '
               'set 0x00000011 ct mark set meta mark comment "lvdc slot yt" # handle 4\n  socket cgroupv2 level 2 '
               '"lvdc.slice/tw" meta mark set 0x00000012 ct mark set meta mark comment "lvdc slot tw" # handle 5\n }\n}')
    assert T.nft_handle_of(yt, listing) == 4 and T.nft_handle_of(T.SLOTS["twitch"], listing) == 5
    assert T.nft_handle_of(T.SLOTS["tiktok"], listing) is None


def test_records_and_parsers():
    assert T.concurrency_record(None) == {"mode": "single"}
    r = T.concurrency_record(T.slot_of("tt"), link_capacity_kbit=104100.0)
    assert r["mode"] == "trio" and r["pool"] == "per_session" and r["mark"] == "0x13" and r["cdp_port"] == 9225
    assert r["ifb_in"] == "lvi_tt" and r["egress_class"] == "1:13" and r["cgroup"] == "lvdc.slice/tt"
    err = ("tcpdump: listening on lvi_yt, link-type EN10MB (Ethernet), snapshot length 262144 bytes\n"
           "48211 packets captured\n48211 packets received by filter\n0 packets dropped by kernel\n")
    assert T.capture_drops(err) == {"captured": 48211, "received": 48211, "dropped_kernel": 0}
    assert T.capture_drops("") == {"captured": None, "received": None, "dropped_kernel": None}
    assert [T.rate_kbit(x) for x in ("575Kbit", "5Mbit", "1Gbit", "9500bit")] == [575.0, 5000.0, 1e6, 9.5]
    assert T.iw_bitrate("\trx bitrate: 866.7 MBit/s VHT-MCS 9\n\ttx bitrate: 650.0 MBit/s") == {"rx_mbit": 866.7,
                                                                                                "tx_mbit": 650.0}
    with tempfile.TemporaryDirectory() as d:
        st = Path(d)
        (st / "tcpdump_in.err").write_text(err)
        (st / "tcpdump_out.err").write_text(err.replace("0 packets dropped", "3 packets dropped"))
        assert T.capture_drops_of(st)["dropped_kernel_total"] == 3
        (st / "tcpdump_out.err").unlink()
        assert T.capture_drops_of(st)["dropped_kernel_total"] is None


def _proc(root: Path, pid: int, ppid: int, cmd: str, cg: str):
    d = root / str(pid)
    d.mkdir()
    (d / "stat").write_text(f"{pid} (chrome (x)) S {ppid} 1 1 0")
    (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode())
    (d / "cgroup").write_text(cg + "\n")


def test_socket_owner():
    yt = T.SLOTS["youtube"]
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _proc(root, 100, 1, "/opt/google/chrome/chrome --remote-debugging-port=9223", "0::/lvdc.slice/yt")
        _proc(root, 101, 100, "chrome --type=zygote", "0::/lvdc.slice/yt")
        _proc(root, 102, 100, "chrome --type=utility --utility-sub-type=network.mojom.NetworkService",
              "0::/lvdc.slice/yt")
        _proc(root, 103, 101, "chrome --type=renderer", "0::/lvdc.slice/yt")
        _proc(root, 200, 1, "chrome --remote-debugging-port=9224", "0::/lvdc.slice/tw")
        rec = T.socket_owner(100, yt, proc=root)
        assert rec["processes"] == 4 and rec["network_service_pids"] == [102] and rec["all_in_slot"]
        assert rec["network_service_cgroups"] == ["0::/lvdc.slice/yt"] and rec["outside_slot"] == []
        (root / "102" / "cgroup").write_text("0::/user.slice/user-1000.slice/session-2.scope\n")
        rec = T.socket_owner(100, yt, proc=root)
        assert not rec["all_in_slot"] and rec["outside_slot"][0]["pid"] == 102
        assert T.socket_owner(999, yt, proc=root)["why"] == "chrome pid not running"
        assert T.socket_owner(None, yt, proc=root)["chrome_pid"] is None


def test_tc_sampler():
    yt = T.SLOTS["youtube"]
    seq = iter([(0, 0, 0), (1514, 0, 125000), (0, 2, 250000), (0, 2, 375000)])
    state = {}

    def sh(cmd):
        if cmd == "tc -s class show dev lvi_yt":
            state["cur"] = next(seq)
            b, dr, by = state["cur"]
            return (f"class htb 1:10 root leaf 8001: prio 0 rate 5Mbit ceil 5Mbit burst 1600b cburst 1600b \n"
                    f" Sent {by} bytes 10 pkt (dropped {dr}, overlimits 0 requeues 0) \n backlog {b}b 1p requeues 0\n")
        if cmd == "tc class show dev lvi_tw":
            return "class htb 1:10 root leaf 8002: prio 0 rate 9Mbit ceil 9Mbit burst 1600b cburst 1600b \n"
        return ""
    with tempfile.TemporaryDirectory() as d:
        net = Path(d) / "net"
        for dev in ("lvi_yt", "lvi_tw"):
            (net / dev).mkdir(parents=True)
        s = T.TcSampler(Path(d), yt, capacity_kbit=12000.0, sh=sh, net=net)
        for _ in range(4):
            row = s.sample()
        assert row["set_rates_kbit"] == {"yt": 5000.0, "tw": 9000.0} and row["flagged"]
        sm = s.summary()
        assert sm["samples"] == 4 and sm["shaper_bound_share"] == round(2 / 3, 3) and sm["ingress_drops"] == 2
        assert sm["flagged_share"] == 1.0 and sm["qa_flag"]
        assert len((Path(d) / "tc_stats.jsonl").read_text().splitlines()) == 4
        s2 = T.TcSampler(Path(d), yt, capacity_kbit=None, sh=lambda c: "", net=net)
        s2.sample()
        assert s2.summary()["samples"] == 0


def test_siblings_and_floor():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "trio_slots.jsonl"
        for r in ({"event": "trio_up", "t": 0}, {"event": "start", "slot": "yt", "session": "yt_1", "t": 10},
                  {"event": "start", "slot": "tw", "session": "tw_1", "t": 40},
                  {"event": "start", "slot": "tt", "session": "ti_1", "t": 70},
                  {"event": "exit", "slot": "tt", "rc": 1, "t": 90},          # crashed: no end of its own
                  {"event": "end", "slot": "tw", "session": "tw_1", "t": 1500},
                  {"event": "exit", "slot": "tw", "rc": 0, "t": 1510},
                  {"event": "start", "slot": "tw", "session": "tw_2", "t": 1600}):
            T.slots_log_append(log, r)
        sib = {x["session"]: x for x in T.siblings(log, "yt_1", 10, 1400)}
        assert set(sib) == {"tw_1", "ti_1"} and sib["ti_1"]["overlap_s"] == 20.0 and sib["ti_1"]["closed_by"] == "exit"
        assert sib["tw_1"]["overlap_s"] == 1360.0 and sib["tw_1"]["rc"] == 0
        assert [x["session"] for x in T.siblings(log, "yt_1", 10, 1700)] == ["ti_1", "tw_1", "tw_2"]
        assert T.siblings(Path(d) / "none.jsonl", "yt_1", 0, 1) == []
        data = Path(d) / "data"
        for rel, n in (("youtube/yt_1/merged.pcap", 2_000_000_000), ("_failed/tiktok/ti_3/merged.pcap", 3_000_000_000)):
            (data / rel).parent.mkdir(parents=True)
            with open(data / rel, "wb") as fh:
                fh.truncate(n)
        fl = T.trio_root_floor_gb(data, 5.0)
        assert fl == {"single_floor_gb": 5.0, "largest_pcap_gb": 3.0, "floor_gb": 14.0}


def test_flow_overlap():
    def rows(pairs):
        return [dict(zip(F.FIELDS, r)) for r in pairs]
    E = ""
    yt = rows([("100.0", "10.0.0.2", "142.250.1.1", E, E, "17", E, E, "50001", "443", "aabbccddeeff0011", E,
                "rr1---sn-x.googlevideo.com", E, E, E),
               ("101.0", "142.250.1.1", "10.0.0.2", E, E, "17", E, E, "443", "50001", "1122334455667788", E, E, E, E, E),
               ("99.0", "10.0.0.2", "8.8.8.8", E, E, "17", E, E, "40000", "53", E, E, E,
                "rr1---sn-x.googlevideo.com", "1", "1")])
    tw = rows([("100.5", "10.0.0.2", "151.101.1.1", E, E, "6", "50100", "443", E, E, E, E,
                "video-weaver.fra02.hls.ttvnw.net", E, E, E)])
    dflt = rows([("98.0", "127.0.0.53", "10.0.0.2", E, E, "17", E, E, "53", "41000", E, E, E,
                  "video-weaver.fra02.hls.ttvnw.net", "1", "2")])
    s = {"youtube/yt_1": {"platform": "youtube", "window": [95.0, 200.0], "s": F.summarise(yt)},
         "twitch/tw_1": {"platform": "twitch", "window": [96.0, 200.0], "s": F.summarise(tw)}}
    rep = F.report(s, F.summarise(dflt))
    assert not rep["leak"], rep
    assert rep["sessions"]["youtube/yt_1"]["dns"]["answered_own_pcap"] == 1
    assert rep["sessions"]["twitch/tw_1"]["dns"]["answered_default_capture"] == 1
    assert F.family("rr1---sn-x.googlevideo.com") == "youtube" and F.family("www.google.com") is None
    # a leak: the tw session also holds a yt flow, a QUIC CID and a yt SNI
    tw_leak = tw + rows([("100.5", "10.0.0.2", "142.250.1.1", E, E, "17", E, E, "50001", "443", "aabbccddeeff0011",
                          E, "rr1---sn-x.googlevideo.com", E, E, E)])
    s["twitch/tw_1"]["s"] = F.summarise(tw_leak)
    rep = F.report(s, F.summarise(dflt))
    assert rep["leak"] and rep["pairs"][0]["shared_flows"] == 1 and rep["pairs"][0]["shared_quic_cids"] == 1
    assert rep["sessions"]["twitch/tw_1"]["sibling_family_snis"] == {"youtube": ["rr1---sn-x.googlevideo.com"]}
    # L-A3: yt media in the default class during the yt session
    s["twitch/tw_1"]["s"] = F.summarise(tw)
    rep = F.report(s, F.summarise(dflt + rows([("120.0", "10.0.0.2", "142.250.9.9", E, E, "6", "50200", "443", E, E,
                                                     E, E, "rr5---sn-y.googlevideo.com", E, E, E)])))
    assert rep["leak"] and rep["sessions"]["youtube/yt_1"]["default_media_snis_in_window"] == ["rr5---sn-y.googlevideo.com"]


class FakeProc:
    def __init__(self, polls: int, rc: int, log: list):
        self.left, self.rc, self.log, self.signals = polls, rc, log, []

    def poll(self):
        self.left -= 1
        return self.rc if self.left < 0 else None

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        return self.rc


def _trio(tmp: Path, targets, spawn, max_per_slot=1, drain_after=None, pf_ok=True, polls=None):
    calls = []
    saved = {k: getattr(O, k) for k in ("_RUNS_DIR", "_pick", "session_matrix", "accepted_sessions", "report_flags",
                                        "_summary", "preclean", "most_needed_state", "coupled_geometry")}
    saved_pf, saved_nft, saved_stage = O.preflight.run, T.nft_handle_of, T.STAGING
    O._RUNS_DIR = tmp
    O._pick = lambda p, nl, now, rg: (None, None, None)
    O.session_matrix = lambda p, f=None: calls.append(("matrix", p, f)) or "A"
    O.accepted_sessions = lambda p: 0
    O.report_flags = lambda p, t: []
    O._summary = lambda: None
    O.preclean = lambda: calls.append(("preclean",))
    O.most_needed_state = lambda *a, **k: None
    O.coupled_geometry = lambda p, g: g
    O.preflight.run = lambda *a, **k: {"ok": pf_ok, "problems": [] if pf_ok else ["rootfs"],
                                       "checks": {"rootfs": {"ok": pf_ok}}, "kw": k}
    T.nft_handle_of = lambda s, listing=None: None
    T.STAGING = tmp / "staging"
    n = {"sleeps": 0}

    def sleep(_):
        n["sleeps"] += 1
        if drain_after is not None and n["sleeps"] == drain_after:
            (tmp / O.TRIO_DRAIN_FILE).write_text("")
        assert n["sleeps"] < 200, "run_trio did not end"
    try:
        rc = O.run_trio(targets, "A", iface=IF, capacity_kbit=104100.0, max_per_slot=max_per_slot, stagger_s=0,
                        spawn=spawn, apply=lambda cmds, check=True: calls.append(("apply", cmds[0], check)) or
                        [{"cmd": c, "rc": 0} for c in cmds],
                        make=lambda args: calls.append(("make", args[0])) or 0, sleep=sleep)
    finally:
        for k, v in saved.items():
            setattr(O, k, v)
        O.preflight.run, T.nft_handle_of, T.STAGING = saved_pf, saved_nft, saved_stage
    return rc, calls


def test_run_trio():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        spawned = []

        def spawn(cmd, env, log):
            spawned.append((cmd, env, log))
            return FakeProc({"youtube": 3, "twitch": 1, "tiktok": 2}[cmd[cmd.index("--platform") + 1]], 0, [])
        rc, calls = _trio(tmp, {"youtube": 5, "twitch": 5, "tiktok": 5}, spawn)
        assert rc == 0 and len(spawned) == 3
        kinds = [c[0] for c in calls]
        up = next(i for i, c in enumerate(calls) if c[0] == "apply" and c[1].startswith("modprobe"))
        assert kinds[0] == "preclean" and up < kinds.index("matrix")          # shared tree before any session
        assert calls[-1][0] == "apply" and calls[-1][1].startswith("tc qdisc del") and calls[-1][2] is False
        assert [c for c in calls if c[0] == "make"] == [("make", "start-capture"), ("make", "stop-capture"),
                                                        ("make", "merge-capture")]
        for cmd, env, log in spawned:
            tag = cmd[cmd.index("--slot") + 1]
            assert T.slot_of(tag).platform == cmd[cmd.index("--platform") + 1] and "--auto" in cmd
            assert cmd[cmd.index("--link-capacity-kbit") + 1] == "104100.0"
            assert env["LVQOE_SHOT_SOURCE"] == "cdp" and env["LVDC_PREFLIGHT_JSON"].endswith(f"preflight_last_{tag}.json")
            assert env["LVDC_TRIO_RUN"] and log.name == f"{tag}_1.log"
        assert [c[2] for c in calls if c[0] == "matrix"] == [f"_summaries/coverage_last_{t}.json" for t in ("yt", "tw", "tt")]
        ev = [json.loads(x) for x in (tmp / T.SLOTS_LOG).read_text().splitlines()]
        assert ev[0]["event"] == "trio_up" and ev[-1]["event"] == "trio_down" and ev[-1]["end_rc"] == 0
        assert sorted(e["slot"] for e in ev if e["event"] == "exit") == ["tt", "tw", "yt"]

        # rolling: no max -> a slot is refilled when its session ends, until drained
        spawned.clear()
        rc, _ = _trio(tmp, {"twitch": 5}, spawn, max_per_slot=None, drain_after=6)
        assert rc == 0 and len(spawned) >= 2 and all("--slot" in c for c, _, _ in spawned)
        assert not (tmp / O.TRIO_DRAIN_FILE).exists() and list((tmp / "_summaries").glob("trio_drain.done_*"))

        # preflight failure: nothing starts, the trio ends with EXIT_PREFLIGHT, the tree still comes down
        spawned.clear()
        rc, calls = _trio(tmp, {"youtube": 5}, spawn, pf_ok=False)
        assert rc == O.EXIT_PREFLIGHT and not spawned and calls[-1][1].startswith("tc qdisc del")

        # a spawn failure in one slot drains: the running sibling finishes before the teardown
        order = []

        def spawn2(cmd, env, log):
            if "twitch" in cmd:
                raise OSError("boom")
            p = FakeProc(4, 0, order)
            order.append("yt started")
            return p
        rc, calls = _trio(tmp, {"youtube": 5, "twitch": 5}, spawn2)
        assert rc == 1 and order == ["yt started"]
        ev = [json.loads(x) for x in (tmp / T.SLOTS_LOG).read_text().splitlines()]
        last_run = ev[max(i for i, e in enumerate(ev) if e["event"] == "trio_up"):]
        assert [e["event"] for e in last_run] == ["trio_up", "exit", "trio_down"]


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_trio_slots: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
