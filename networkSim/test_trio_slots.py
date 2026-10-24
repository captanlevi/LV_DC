#!/usr/bin/env python3
"""t53 --concurrent: trio_slots.py (the slot tree = gate 1's tree, each slot's teardown touches only its own pieces,
the socket-owner statement, tc samples, the sibling timeline, the A2 rootfs floor), flow_overlap.py (5-tuple / QUIC CID
uniqueness, sibling families, default-class media, DNS presence) and orchestrate.run_trio (rolling slots, stagger,
drain, preflight / spawn failures drain instead of tearing down under running siblings). No root, no network.

    python3 test_trio_slots.py        (exit 0 = pass)
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LVDC_MGMT_UDP_PORT", "41641")   # t58 L-6: the goldens hold the default port

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orchestrate as O                                  # noqa: E402


def test_single_mode_does_not_import_trio_slots():
    """In a fresh interpreter: under pytest another test module may already have imported trio_slots."""
    import subprocess
    r = subprocess.run([sys.executable, "-c", "import sys, orchestrate; "
                        "sys.exit('trio_slots' in sys.modules)"], cwd=HERE, capture_output=True, text=True)
    assert r.returncode == 0, ("single mode imports trio_slots", r.stderr[-500:])
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
    up = T.slot_up_cmds(T.SLOTS["yt1"], IF)
    assert up[0] == "mkdir -p /sys/fs/cgroup/lvdc.slice/yt1" and up[1].startswith("nft add rule")


def test_leak_fixes():
    """pilot-0 read (levi 1a / 1b): socketless egress packets take their connection's mark from conntrack (our marks
    only); a session's mark carries its epoch, so the slot's previous session's conntrack marks miss its filters; every
    Chrome process is swept back into the slot's cgroup."""
    up = T.shared_up_cmds(IF)
    restore = [c for c in up if "lvdc restore" in c]
    assert len(restore) == 1 and "meta mark 0 ct mark & 0xf0 == 0x10 meta mark set ct mark" in restore[0]
    assert up.index(restore[0]) > up.index(next(c for c in up if "nft add chain" in c))
    yt = T.SLOTS["yt1"]
    e3 = yt.with_epoch(100003)
    assert (e3.mark, e3.egress_class, e3.prio, e3.tag) == (0x311, "1:311", 10, "yt1") and e3.record()["epoch"] == 3
    assert yt.with_epoch(16).mark == 0x11 and T.SLOTS["tt1"].with_epoch(5).mark == 0x513
    eg = [c for c in up if "parent 1: protocol all prio 5 matchall action connmark continue" in c]
    assert len(eg) == 1 and up.index(eg[0]) > max(i for i, c in enumerate(up) if "parent 1: protocol ip prio 1" in c)
    drop = [c for c in up if "handle 0x10/0xf0 fw action drop" in c]
    assert len(drop) == 2 and all("prio 40" in c for c in drop)           # ingress and egress, after the slots' prios
    assert T.slot_detach_cmds(yt, IF) == ["tc filter del dev wlp0s20f3 parent ffff: prio 10",
                                          "tc filter del dev wlp0s20f3 parent 1: prio 10"]
    j = "\n".join(T.slot_up_cmds(e3, IF))
    assert "meta mark set 0x311 ct mark set meta mark" in j and "handle 0x311 fw" in j and "flowid 1:311" in j
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _proc(root, 100, 1, "/opt/google/chrome/chrome --remote-debugging-port=9223",
              "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-com.google.Chrome-100.scope")
        _proc(root, 101, 100, "chrome --type=utility --utility-sub-type=network.mojom.NetworkService", "0::/lvdc.slice/yt1")
        _proc(root, 102, 100, "chrome --type=utility --utility-sub-type=audio.mojom.AudioService",
              "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-com.google.Chrome-100.scope")
        moved = []

        def mover(pid):
            moved.append(pid)
            (root / str(pid) / "cgroup").write_text("0::/lvdc.slice/yt1\n")
            return True
        sw = T.CgroupSweeper(yt, lambda: 100, proc=root, mover=mover)
        assert sw.sweep() == 2 and moved == [100, 102]
        assert sw.sweep() == 0                                       # all in the slot now
        rec = sw.stop()
        assert rec["n_moves"] == 2 and rec["sweeps"] == 2 and rec["moves"][0]["from"].endswith("Chrome-100.scope")
        assert T.socket_owner(100, yt, proc=root)["all_in_slot"]
        assert T.CgroupSweeper(yt, lambda: None, proc=root, mover=mover).sweep() == 0


def test_slot_down_touches_only_its_own():
    yt = T.SLOTS["yt1"]
    down = "\n".join(T.slot_down_cmds(yt, IF, nft_handle=7))
    for o in ("tw", "tt"):
        assert f"lvi_{o}" not in down and f"lvo_{o}" not in down and f"lvdc.slice/{o}" not in down
    assert "prio 11" not in down and "prio 12" not in down and "1:12" not in down and "1:13" not in down
    assert "cgroup.kill" in down.splitlines()[0]                         # leftovers killed before anything else
    assert "nft delete rule inet lvdc_trio out handle 7" in down
    assert "qdisc del dev wlp0s20f3 root" not in down and "ingress" not in down.replace("parent ffff:", "")
    assert "ifb0" not in down and "ifb1" not in down
    listing = ('table inet lvdc_trio {\n chain out { # handle 1\n  socket cgroupv2 level 2 "lvdc.slice/yt1" meta mark '
               'set 0x00000011 ct mark set meta mark comment "lvdc slot yt1" # handle 4\n  socket cgroupv2 level 2 '
               '"lvdc.slice/tw1" meta mark set 0x00000012 ct mark set meta mark comment "lvdc slot tw1" # handle 5\n }\n}')
    assert T.nft_handle_of(yt, listing) == 4 and T.nft_handle_of(T.SLOTS["tw1"], listing) == 5
    assert T.nft_handle_of(T.SLOTS["tt1"], listing) is None


def test_records_and_parsers():
    assert T.concurrency_record(None) == {"mode": "single"}
    r = T.concurrency_record(T.slot_of("tt1"), link_capacity_kbit=104100.0)
    assert r["mode"] == "trio" and r["pool"] == "per_session" and r["mark"] == "0x13" and r["cdp_port"] == 9225
    assert r["ifb_in"] == "lvi_tt1" and r["egress_class"] == "1:13" and r["cgroup"] == "lvdc.slice/tt1"
    err = ("tcpdump: listening on lvi_yt1, link-type EN10MB (Ethernet), snapshot length 262144 bytes\n"
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
    yt = T.SLOTS["yt1"]
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _proc(root, 100, 1, "/opt/google/chrome/chrome --remote-debugging-port=9223", "0::/lvdc.slice/yt1")
        _proc(root, 101, 100, "chrome --type=zygote", "0::/lvdc.slice/yt1")
        _proc(root, 102, 100, "chrome --type=utility --utility-sub-type=network.mojom.NetworkService",
              "0::/lvdc.slice/yt1")
        _proc(root, 103, 101, "chrome --type=renderer", "0::/lvdc.slice/yt1")
        _proc(root, 200, 1, "chrome --remote-debugging-port=9224", "0::/lvdc.slice/tw1")
        rec = T.socket_owner(100, yt, proc=root)
        assert rec["processes"] == 4 and rec["network_service_pids"] == [102] and rec["all_in_slot"]
        assert rec["network_service_cgroups"] == ["0::/lvdc.slice/yt1"] and rec["outside_slot"] == []
        (root / "102" / "cgroup").write_text("0::/user.slice/user-1000.slice/session-2.scope\n")
        rec = T.socket_owner(100, yt, proc=root)
        assert not rec["all_in_slot"] and rec["outside_slot"][0]["pid"] == 102
        assert T.socket_owner(999, yt, proc=root)["why"] == "chrome pid not running"
        assert T.socket_owner(None, yt, proc=root)["chrome_pid"] is None


def test_tc_sampler():
    yt = T.SLOTS["yt1"]
    seq = iter([(0, 0, 0), (1514, 0, 125000), (0, 2, 250000), (0, 2, 375000)])
    state = {}

    def sh(cmd):
        if cmd == "tc -s class show dev lvi_yt1":
            state["cur"] = next(seq)
            b, dr, by = state["cur"]
            return (f"class htb 1:10 root leaf 8001: prio 0 rate 5Mbit ceil 5Mbit burst 1600b cburst 1600b \n"
                    f" Sent {by} bytes 10 pkt (dropped {dr}, overlimits 0 requeues 0) \n backlog {b}b 1p requeues 0\n")
        if cmd == "tc class show dev lvi_tw1":
            return "class htb 1:10 root leaf 8002: prio 0 rate 9Mbit ceil 9Mbit burst 1600b cburst 1600b \n"
        return ""
    with tempfile.TemporaryDirectory() as d:
        net = Path(d) / "net"
        for dev in ("lvi_yt1", "lvi_tw1"):
            (net / dev).mkdir(parents=True)
        s = T.TcSampler(Path(d), yt, capacity_kbit=12000.0, sh=sh, net=net)
        for _ in range(4):
            row = s.sample()
        assert row["set_rates_kbit"] == {"yt1": 5000.0, "tw1": 9000.0} and row["flagged"]
        sm = s.summary()
        assert sm["samples"] == 4 and sm["shaper_bound_share"] == round(2 / 3, 3) and sm["ingress_drops"] == 2
        assert sm["flagged_share"] == 1.0 and sm["qa_flag"]
        assert len((Path(d) / "tc_stats.jsonl").read_text().splitlines()) == 4
        s2 = T.TcSampler(Path(d), yt, capacity_kbit=None, sh=lambda c: "", net=net)
        s2.sample()
        assert s2.summary()["samples"] == 0


def test_trio_root_names_and_geometry():
    import geometry
    import session_names
    with tempfile.TemporaryDirectory() as d:
        data = Path(d)
        a = session_names.next_session_dir(data, "youtube", "yt")
        b = session_names.next_session_dir(data / "_trio", "youtube", "yt", floor=T.TRIO_NAME_FLOOR)
        c = session_names.next_session_dir(data, "youtube", "yt")
        assert (a, b, c) == (data / "youtube/yt_1", data / "_trio/youtube/yt_100001", data / "youtube/yt_2")
        assert json.loads((data / "_summaries/session_counter_youtube.json").read_text())["last"] == 2   # single's own
        assert json.loads((data / "_trio/_summaries/session_counter_youtube.json").read_text())["last"] == 100001
        (data / "_trio/_failed/youtube/yt_100009").mkdir(parents=True)                  # a trio name is never reused
        assert session_names.next_session_dir(data / "_trio", "youtube", "yt", floor=T.TRIO_NAME_FLOOR).name == "yt_100010"
        # the pre-start preflight
        repo = data / "repo"
        for d in (".venv/bin", "current_data", "labeling", "profiles/youtube"):
            (repo / d).mkdir(parents=True)
        (repo / ".venv/bin/python3").write_text("")
        (repo / "labeling/manifest.json").write_text("{}")
        pf = T.trio_preflight(["youtube", "twitch"], repo, repo / "profiles", data, tools=("sh",))
        assert not pf["ok"] and pf["problems"] == ["profile_template_twitch"], pf
        (repo / "profiles/twitch").mkdir()
        assert T.trio_preflight(["youtube", "twitch"], repo, repo / "profiles", data, tools=("sh",))["ok"]
        assert all(not g.startswith("fullscreen") for _ in range(6)
                   for g in [geometry.next_geometry(data, "youtube", ("fullscreen",))])
        assert "fullscreen:1920x1080" in geometry.GEOMETRY_CHOICES["youtube"]          # single mode's rotation unchanged


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
        fl = T.staging_floor_gb(data, 25.0, n_slots=3)
        assert {k: fl[k] for k in ("base_gb", "largest_pcap_gb", "n_slots", "floor_gb")} == \
            {"base_gb": 25.0, "largest_pcap_gb": 3.0, "n_slots": 3, "floor_gb": 34.0}, fl
        assert fl["applies_to"] == "data volume (trio staging)" and fl["staging"] == str(T.STAGING)
        # t58: the counted trio root's pcaps count, decode_stress's (held top state) never; the floor scales with N
        for rel, n in (("_trio/twitch/tw_100001/merged.pcap", 4_000_000_000),
                       ("_trio_stress/twitch/tw_900001/merged.pcap", 9_000_000_000)):
            (data / rel).parent.mkdir(parents=True)
            with open(data / rel, "wb") as fh:
                fh.truncate(n)
        fl = T.staging_floor_gb(data, 25.0, n_slots=5)
        assert (fl["largest_pcap_gb"], fl["floor_gb"]) == (4.0, 45.0), fl
        assert T.staging_floor_gb(data, 25.0)["n_slots"] == len(T.SLOTS)
        # the staged capture is writing (levi): every file past the pcap header
        st = Path(d) / "stage"
        st.mkdir()
        (st / "incoming.pcap").write_bytes(b"x" * 24)
        assert T.capture_growing(st) == {"sizes": {"incoming.pcap": 24, "outgoing.pcap": None}, "ok": False}
        (st / "incoming.pcap").write_bytes(b"x" * 200)
        (st / "outgoing.pcap").write_bytes(b"x" * 100)
        assert T.capture_growing(st)["ok"] is True
        assert "TCPDUMP_Z=-Z root" in T.capture_vars(st) and T.STAGING.parent.resolve() == T.DATA.resolve()


def _pkt(t, src, dst, proto, sport, dport, n=100, dcid="", sni="", qname="", resp="", ans=""):
    r = dict.fromkeys(F.FIELDS, "")
    r.update({"frame.time_epoch": str(t), "frame.len": str(n), "ip.src": src, "ip.dst": dst, "quic.dcid": dcid,
              "tls.handshake.extensions_server_name": sni, "dns.qry.name": qname, "dns.flags.response": resp,
              "dns.count.answers": ans})
    r["tcp.srcport" if proto == "tcp" else "udp.srcport"] = str(sport)
    r["tcp.dstport" if proto == "tcp" else "udp.dstport"] = str(dport)
    return r


def test_window_fits_edges():
    """Restart §10 at the edge (hardening stop): a session whose length + the allowance ends exactly at the deadline
    fits; a microsecond more idles; the allowance counts (levi 00138: 180 s)."""
    d = 10_000.0
    assert O.window_fits(d - 600 - 180, 10.0, d) and not O.window_fits(d - 600 - 180 + 1e-6, 10.0, d)
    assert O.window_fits(d - 600, 10.0, d, allowance_s=0.0) and not O.window_fits(d - 600, 10.0, d)
    assert O.WINDOW_ALLOWANCE_S == 180.0 and not O.window_fits(d, 0.0, d)        # no time left: nothing starts
    src = (HERE / "orchestrate.py").read_text()
    assert 'not window_fits(time.time(), slen["minutes"], window["deadline"])' in src


def test_uplink_cap():
    """levi 02:48 (diag only): the uplink cap is one ingress policer after the management bypass and the connmark restore
    and before every slot filter and the default class, dropping the excess and continuing classification for the rest;
    its bucket is 50 ms at the cap (>= 64 KiB); orchestrate applies it after the shared tree and records it in trio_up;
    the flag needs --trio-stress."""
    c = T.uplink_cap_cmds("wlp0s20f3", 40000)
    assert c == ["tc filter add dev wlp0s20f3 parent ffff: protocol all prio 6 matchall action police rate 40000kbit "
                 "burst 250000 mtu 64kb conform-exceed drop/continue"], c
    assert "burst 65536 " in T.uplink_cap_cmds("x", 1000)[0]
    up = " ".join(T.shared_up_cmds("wlp0s20f3"))
    prios = [int(x) for x in __import__("re").findall(r"parent ffff: protocol \w+ prio (\d+)", up)]
    assert min(prios) == 1 and 5 in prios and all(p < T.UPLINK_CAP_PRIO for p in prios if p <= 5)
    assert all(p > T.UPLINK_CAP_PRIO for p in prios if p > 5)                    # orphan drop 40, default 50
    assert T.SLOTS == {} or all(s.prio > T.UPLINK_CAP_PRIO for s in T.SLOTS.values())
    assert all(10 + k > T.UPLINK_CAP_PRIO for k in range(T.MAX_SLOTS))
    src = (HERE / "orchestrate.py").read_text()
    assert src.index("apply(T.shared_up_cmds(iface))") < src.index("apply(uplink_cap[\"cmds\"])") < src.index('"uplink_cap": uplink_cap')

def test_dns_answers():
    txt = "rr1---sn-x.googlevideo.com\t142.250.1.1,142.250.1.2\t\nwww.youtube.com\t142.250.1.1\t\n\t\t\nnoans.com\t\t\n"
    assert T.parse_dns(txt) == {"142.250.1.1": ["rr1---sn-x.googlevideo.com", "www.youtube.com"],
                                "142.250.1.2": ["rr1---sn-x.googlevideo.com"]}
    td = ("12:00:00.000000 IP 10.0.0.2.50001 > 142.250.1.1.443: UDP, length 1300\n"
          "12:00:00.1 IP 151.101.1.1.443 > 10.0.0.2.50100: tcp 900\n12:00:01 IP 10.0.0.2 > 8.8.8.8: ICMP echo request\n")
    assert T.contacted_addresses(td) == {"10.0.0.2", "142.250.1.1", "151.101.1.1"}
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        own, dd, early = d / "merged.pcap", d / "dns_default.pcap", d / "early.pcap"
        for f in (own, dd, early):
            f.write_bytes(b"")
        fake = {str(own): "", str(dd): "video-weaver.x.ttvnw.net\t151.101.1.1\t\nother.com\t9.9.9.9\t\n",
                str(early): "rr1---sn-x.googlevideo.com\t142.250.1.1\t\n"}
        saved = T.tshark_dns, T.subprocess.run
        T.tshark_dns = lambda pc, ts="tshark": fake[str(pc)]
        T.subprocess.run = lambda *a, **k: type("R", (), {"stdout": td, "returncode": 0})()
        try:
            ans, rec = T.dns_answers_for(own, [dd, early])
        finally:
            T.tshark_dns, T.subprocess.run = saved
        # only addresses the session's own pcap contacted; an earlier session's answer names a cached address
        assert ans == {"151.101.1.1": ["video-weaver.x.ttvnw.net"], "142.250.1.1": ["rr1---sn-x.googlevideo.com"]}, ans
        assert rec["default_kept"] == 2 and rec["default_addresses"] == 3
        troot = d / "_trio"
        (troot / "youtube" / "yt_4" / "trio").mkdir(parents=True)
        (troot / "youtube" / "yt_4" / "trio" / "dns_default.pcap").write_bytes(b"")
        (troot / "youtube" / "yt_4" / "dns_default.pcap").write_bytes(b"")     # an old layout's file: not read
        (troot / "_failed" / "twitch" / "tw_5" / "trio").mkdir(parents=True)
        (troot / "_failed" / "twitch" / "tw_5" / "trio" / "dns_default.pcap").write_bytes(b"")
        log = troot / "_summaries" / "trio_slots.jsonl"
        for r in ({"event": "start", "session": "yt_4", "platform": "youtube", "run": "R", "t": 1},
                  {"event": "start", "session": "tw_5", "platform": "twitch", "run": "R", "t": 2},
                  {"event": "start", "session": "ti_6", "platform": "tiktok", "run": "R", "t": 3},
                  {"event": "start", "session": "yt_1", "platform": "youtube", "run": "OLD", "t": 0},
                  {"event": "end", "session": "yt_4", "t": 5}, {"event": "end", "session": "tw_5", "t": 6}):
            T.slots_log_append(log, r)
        got = T.run_dns_pcaps(log, troot, "R", "yt_7")
        assert got == sorted([troot / "_failed/twitch/tw_5/trio/dns_default.pcap", troot / "youtube/yt_4/trio/dns_default.pcap"])
        # flow_overlap reads dns_answers.json: a contacted media address with no name is a DNS fail
        me = "10.0.0.2"
        s = F.summarise([_pkt(100.0, me, "142.250.1.1", "udp", 50001, 443, 1300, "aabbccddeeff0011",
                              "rr1---sn-x.googlevideo.com")])
        (d / "dns_answers.json").write_text(json.dumps({"142.250.1.1": ["rr1---sn-x.googlevideo.com"]}))
        rep = F.report({"youtube/yt_7": {"platform": "youtube", "window": [0, 1e12], "s": s,
                                         "dns_answers_file": str(d / "dns_answers.json")}}, None)
        assert rep["dns_fail"] == [] and rep["sessions"]["youtube/yt_7"]["dns_answers_json"]["media_unnamed"] == []
        (d / "dns_answers.json").write_text(json.dumps({}))
        rep = F.report({"youtube/yt_7": {"platform": "youtube", "window": [0, 1e12], "s": s,
                                         "dns_answers_file": str(d / "dns_answers.json")}}, None)
        assert rep["dns_fail"] == ["youtube/yt_7"]


def test_check_session_reads_dns_answers():
    """pilot-0 fix 2 (levi): a flow with no SNI and no DNS in its own pcap is named from the session's dns_answers.json
    (yt_100001's 88 MB QUIC media flow read as exogenous background); the pcap's own sources come first."""
    import check_session as CS
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        assert CS.dns_answers_names(sd) == {}
        (sd / "dns_answers.json").write_text(json.dumps({"144.6.4.207": ["RR1---sn-X.googlevideo.com"]}))
        ans = CS.dns_answers_names(sd)
        key = ("192.168.4.50", 443, "144.6.4.207", 50001, "udp")
        names, src = CS.flow_name("144.6.4.207", key, {}, {}, {}, ans)
        assert (names, src) == ({"rr1---sn-x.googlevideo.com"}, "dns_answers")
        assert CS.flow_category(names, "youtube") == "media"
        assert CS.flow_name("144.6.4.207", key, {}, {}, {"144.6.4.207": {"a.example"}}, ans)[1] == "tshark_dns"
        assert CS.flow_name("144.6.4.207", key, {key: "b.example"}, {}, {}, ans)[1] == "network_core"
        assert CS.flow_name("1.2.3.4", key, {}, {}, {}, ans) == (set(), "none")
        (sd / "dns_answers.json").write_text("not json")
        assert CS.dns_answers_names(sd) == {}


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


def _trio(tmp: Path, targets, spawn, max_per_slot=1, drain_after=None, pf_ok=True, polls=None, layout="cdp",
          xrandr=None, trio_pf=None, disk_gb=1000.0, offloads=None, slots=None, coupled=None, n_draw=None,
          window_min=None, matrix="A"):
    calls = []
    saved = {k: getattr(O, k) for k in ("_RUNS_DIR", "_pick", "session_matrix", "accepted_sessions", "report_flags",
                                        "_summary", "preclean", "most_needed_state", "coupled_geometry", "free_gb")}
    O.free_gb = lambda path: disk_gb            # the test's disk, not the machine's (flaky at 23 GB free on the laptop)
    saved_pf, saved_nft, saved_stage, saved_tpf = O.preflight.run, T.nft_handle_of, T.STAGING, T.trio_preflight
    saved_ct = T.conntrack_timeouts
    T.conntrack_timeouts = lambda proc=None: {"nf_conntrack_udp_timeout_stream": 120}
    T.trio_preflight = trio_pf or (lambda *a, **k: {"ok": True, "problems": [], "checks": {}})
    O._RUNS_DIR = tmp
    O._pick = lambda p, nl, now, rg, ex=(), root=None, siblings=frozenset(), probe=None: \
        calls.append(("pick", p, ex, root, siblings)) and None or (None, None, None)
    O.session_matrix = (lambda p, f=None, ex=(), data_dir=None, select=None, reserved=None, key=None, couple_geometry=True:
                        calls.append(("matrix", p, f, ex, data_dir, select, reserved, key, couple_geometry)) or matrix)
    O.accepted_sessions = lambda p, root=None, select=None: calls.append(("accepted", p, root)) and 0 or 0
    O.report_flags = lambda p, t, root=None, only=None: []
    O._summary = lambda: None
    O.preclean = lambda: calls.append(("preclean",))
    O.most_needed_state = lambda *a, **k: None
    O.coupled_geometry = coupled or (lambda p, g: g)
    O.preflight.run = lambda *a, **k: {"ok": pf_ok, "problems": [] if pf_ok else ["rootfs"],
                                       "checks": {"rootfs": {"ok": pf_ok}}, "kw": k}
    T.nft_handle_of = lambda s, listing=None: None
    T.STAGING = tmp / "staging"
    n = {"sleeps": 0}

    def sleep(_):
        n["sleeps"] += 1
        if drain_after is not None and n["sleeps"] == drain_after:
            (tmp / T.TRIO_DIR / O.TRIO_DRAIN_FILE).write_text("")
        assert n["sleeps"] < 200, "run_trio did not end"
    try:
        rc = O.run_trio(targets, "A", iface=IF, capacity_kbit=104100.0, max_per_slot=max_per_slot, stagger_s=0,
                        spawn=spawn, apply=lambda cmds, check=True: calls.append(("apply", cmds[0], check)) or
                        [{"cmd": c, "rc": 0} for c in cmds],
                        make=lambda args: calls.append(("make", args[0])) or 0, sleep=sleep, layout=layout,
                        xrandr=xrandr, offloads=offloads or (lambda iface: {"iface": iface, "ok": True, "after": {}, "calls": []}),
                        slots=slots, n_draw=n_draw, window_min=window_min)
    finally:
        for k, v in saved.items():
            setattr(O, k, v)
        T.SLOTS.clear()                                  # run_trio's composition is the process's; the tests restore it
        T.SLOTS.update(T.build_slots(T.TRIO_COMPOSITION))
        O.preflight.run, T.nft_handle_of, T.STAGING, T.trio_preflight = saved_pf, saved_nft, saved_stage, saved_tpf
        T.conntrack_timeouts = saved_ct
    return rc, calls


def test_run_trio_records_the_n_draw_and_session_lengths():
    """Restart prereg §1 / §4: the round's draw runs its composition and is recorded in trio_up and (LVDC_N_DRAW) every
    slot's env; each session's length is drawn (seeded by the run, slot and index) and passed as --session-minutes with
    the base matrix's episodes scaled to it."""
    import n_draw
    import record
    from utils import shaping
    d = next(x for x in n_draw.simulate(20, record.ERA) if x["n"] == 4)
    with tempfile.TemporaryDirectory() as t:
        tmp, spawned = Path(t), []

        def spawn(cmd, env, log):
            spawned.append((cmd, env, log))
            return FakeProc(1, 0, [])
        rc, _ = _trio(tmp, {"youtube": 5, "twitch": 5, "tiktok": 5}, spawn, slots=d["composition"], n_draw=d)
        assert rc == 0 and len(spawned) == 4
        ev = [json.loads(x) for x in (tmp / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
        assert ev[0]["event"] == "trio_up" and ev[0]["n_draw"] == d and ev[0]["composition"] == d["composition"]
        run = ev[0]["run"]
        for cmd, env, _ in spawned:
            assert json.loads(env["LVDC_N_DRAW"]) == d
            tag = cmd[cmd.index("--slot") + 1]
            sl = json.loads(env["LVDC_SESSION_LENGTH"])
            assert {k: sl[k] for k in ("rule", "seed", "key", "range_min", "minutes")} == \
                shaping.draw_session_minutes(f"{run}:{tag}:1")
            assert sl["scheduled_s"] == sl["episodes"] * 60.0                         # matrix "A": 60 s episodes
            assert float(cmd[cmd.index("--session-minutes") + 1]) == sl["minutes"]
            assert int(cmd[cmd.index("--episodes") + 1]) == round(sl["minutes"])        # matrix "A": 60 s episodes
        assert n_draw.draw(tmp / T.TRIO_DIR / T.SLOTS_LOG, record.ERA, False)["round"] == 0   # a record draw: not the pilot's
        # v3 (levi 18:58 / 16:49): outside T* a run that started no session is a void, not a draw; the harness's fake
        # sessions never log "start", so the round stays 0 until one does
        assert record.ERA not in n_draw.VOID_COUNTS_ERAS
        assert n_draw.draw(tmp / T.TRIO_DIR / T.SLOTS_LOG, record.ERA, True)["round"] == 0
        T.slots_log_append(tmp / T.TRIO_DIR / T.SLOTS_LOG, {"event": "start", "slot": "yt1", "run": run})
        assert n_draw.draw(tmp / T.TRIO_DIR / T.SLOTS_LOG, record.ERA, True)["round"] == 1
    with tempfile.TemporaryDirectory() as t:                     # no draw: no record, no env
        spawned = []
        rc, _ = _trio(Path(t), {"youtube": 5, "twitch": 5, "tiktok": 5},
                      lambda cmd, env, log: spawned.append((cmd, env)) or FakeProc(1, 0, []))
        ev = json.loads((Path(t) / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()[0])
        assert "n_draw" not in ev and all("LVDC_N_DRAW" not in e for _, e in spawned)


def test_fast_episode_length_drawn_per_session():
    """Restart §2b: an @fast session's episode length is drawn per session (log-U(12, 48) s, the session-length key),
    passed as --fast-episode-s with the episodes scaled to it and recorded in the session-length record; @slow / base
    sessions get none."""
    from utils import shaping
    with tempfile.TemporaryDirectory() as t:
        tmp, spawned = Path(t), []
        rc, _ = _trio(tmp, {"youtube": 5, "twitch": 5}, lambda cmd, env, log: spawned.append((cmd, env)) or
                      FakeProc(1, 0, []), matrix="A@fast")
        assert rc == 0 and len(spawned) == 2
        for cmd, env in spawned:
            sl = json.loads(env["LVDC_SESSION_LENGTH"])
            fe = shaping.draw_fast_episode_s(sl["key"])
            assert sl["fast_episode"] == fe and float(cmd[cmd.index("--fast-episode-s") + 1]) == fe["episode_s"]
            assert int(cmd[cmd.index("--episodes") + 1]) == max(1, round(sl["minutes"] * 60 / fe["episode_s"]))
    with tempfile.TemporaryDirectory() as t:
        spawned = []
        _trio(Path(t), {"youtube": 5}, lambda cmd, env, log: spawned.append((cmd, env)) or FakeProc(1, 0, []),
              matrix="A@slow")
        cmd, env = spawned[0]
        assert "--fast-episode-s" not in cmd and "fast_episode" not in json.loads(env["LVDC_SESSION_LENGTH"])
        assert cmd[cmd.index("--episodes") + 1] == "3"


def test_window_refill_and_idle():
    """Restart §10: a window run refills each slot while its next drawn length fits before the deadline; a slot whose
    next length does not fit idles to the end (window_idle, no truncation, no re-draw); the window is in trio_up and
    every slot's env (LVDC_TRIO_WINDOW)."""
    saved, saved_allow = O.draw_session_minutes, O.WINDOW_ALLOWANCE_S
    try:
        O.WINDOW_ALLOWANCE_S = 0.0                        # the allowance on its own below
        # the first session of each slot is short enough to fit a 0.05 min window, the second never does
        O.draw_session_minutes = lambda key: {"key": key, "minutes": 0.0001 if key.endswith(":1") else 10.0}
        with tempfile.TemporaryDirectory() as t:
            tmp, spawned = Path(t), []
            rc, _ = _trio(tmp, {"youtube": 9, "twitch": 9, "tiktok": 9},
                          lambda cmd, env, log: spawned.append((cmd, env)) or FakeProc(1, 0, []),
                          max_per_slot=None, window_min=0.05)
            ev = [json.loads(x) for x in (tmp / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
            up = ev[0]
            assert rc == 0 and len(spawned) == 3 and up["window"]["min"] == 0.05
            assert abs(up["window"]["deadline"] - up["window"]["t0"] - 3.0) < 1e-6
            assert all(json.loads(e["LVDC_TRIO_WINDOW"]) == up["window"] for _, e in spawned)
            idle = [e for e in ev if e["event"] == "window_idle"]
            assert sorted(e["slot"] for e in idle) == ["tt1", "tw1", "yt1"] and all(e["minutes"] == 10.0 for e in idle)
            assert ev[-1]["event"] == "trio_down"
        with tempfile.TemporaryDirectory() as t:             # a window too short for any first session: none starts
            tmp, spawned = Path(t), []
            O.draw_session_minutes = lambda key: {"key": key, "minutes": 10.0}
            rc, _ = _trio(tmp, {"youtube": 9}, lambda cmd, env, log: spawned.append(cmd) or FakeProc(1, 0, []),
                          max_per_slot=None, window_min=0.05)
            assert rc == 0 and not spawned
        O.WINDOW_ALLOWANCE_S = saved_allow                # levi 00138: 3 min for startup + teardown in the fit check
        with tempfile.TemporaryDirectory() as t:
            tmp, spawned = Path(t), []
            O.draw_session_minutes = lambda key: {"key": key, "minutes": 0.0001}
            rc, _ = _trio(tmp, {"youtube": 9}, lambda cmd, env, log: spawned.append(cmd) or FakeProc(1, 0, []),
                          max_per_slot=None, window_min=3.0)    # 180 s: a 0.006 s session + 180 s does not fit
            ev = [json.loads(x) for x in (tmp / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
            idle = [e for e in ev if e["event"] == "window_idle"]
            assert not spawned and idle[0]["allowance_s"] == 180.0 and idle[0]["reason"] == "does not fit", idle
    finally:
        O.draw_session_minutes, O.WINDOW_ALLOWANCE_S = saved, saved_allow
    with tempfile.TemporaryDirectory() as t:                 # no window: nothing recorded, no env
        spawned = []
        _trio(Path(t), {"youtube": 5}, lambda cmd, env, log: spawned.append(env) or FakeProc(1, 0, []))
        ev = json.loads((Path(t) / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()[0])
        assert "window" not in ev and all("LVDC_TRIO_WINDOW" not in e for e in spawned)


def test_window_runs_to_time_pauses_and_bookkeeping():
    """levi 00138 (§10): inside a window, targets and --max-per-slot never stop a slot (it runs to time); a paused
    slot idles to the end with its reason; more than one paused slot is a stop-and-read (EXIT_WINDOW_READ); trio_down
    records the window's actual end and each slot's busy / idle seconds; a session's scheduled_s is recorded."""
    saved, saved_allow = O.draw_session_minutes, O.WINDOW_ALLOWANCE_S
    try:
        O.WINDOW_ALLOWANCE_S = 0.0
        O.draw_session_minutes = lambda key: {"key": key, "minutes": 10.0 if key.endswith(":3") else 0.0001}
        with tempfile.TemporaryDirectory() as t:         # target 0 and max 1 per slot: still two sessions per slot
            tmp, spawned = Path(t), []
            rc, _ = _trio(tmp, {"youtube": 0, "twitch": 0}, lambda cmd, env, log: spawned.append((cmd, env)) or
                          FakeProc(1, 0, []), max_per_slot=1, window_min=0.05)
            assert rc == 0 and len(spawned) == 4, [c[c.index("--slot") + 1] for c, _ in spawned]
            ev = [json.loads(x) for x in (tmp / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
            down = ev[-1]
            assert down["event"] == "trio_down" and down["window"]["t_end"] >= down["window"]["t0"]
            w = down["window"]
            assert set(w["busy_s"]) == set(w["idle_s"]) == {"yt1", "tw1", "tt1"}
            assert all(abs(w["busy_s"][k] + w["idle_s"][k] - w["span_s"]) < 0.2 for k in ("yt1", "tw1"))
            for cmd, env in spawned:
                sl = json.loads(env["LVDC_SESSION_LENGTH"])
                assert sl["episodes"] == int(cmd[cmd.index("--episodes") + 1]) and sl["scheduled_s"] == sl["episodes"] * 60.0
        with tempfile.TemporaryDirectory() as t:         # a paused slot (login wall) idles with its reason
            tmp = Path(t)
            rc, _ = _trio(tmp, {"youtube": 9, "twitch": 9, "tiktok": 9}, lambda cmd, env, log: FakeProc(
                1, O.EXIT_LOGIN_WALL if "twitch" in cmd else 0, []), max_per_slot=None, window_min=0.05)
            ev = [json.loads(x) for x in (tmp / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
            tw = [e for e in ev if e["event"] == "window_idle" and e["slot"] == "tw1"]
            assert rc == 0 and len(tw) == 1 and tw[0]["reason"].startswith("paused: login wall"), tw
            assert not [e for e in ev if e["event"] == "window_stop"]
        with tempfile.TemporaryDirectory() as t:         # two paused slots: stop and read
            tmp = Path(t)
            rc, _ = _trio(tmp, {"youtube": 9, "twitch": 9}, lambda cmd, env, log: FakeProc(
                1, O.EXIT_LOGIN_WALL if "twitch" in cmd else 0, []), max_per_slot=None, window_min=0.05,
                slots={"youtube": 1, "twitch": 2})
            ev = [json.loads(x) for x in (tmp / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
            stop = [e for e in ev if e["event"] == "window_stop"]
            assert rc == O.EXIT_WINDOW_READ and len(stop) == 1 and stop[0]["reason"] == "2 slots paused", ev
    finally:
        O.draw_session_minutes, O.WINDOW_ALLOWANCE_S = saved, saved_allow


def test_concurrency_over():
    """Restart §10: the sessions running beside a session (itself included), as a step series and its time-weighted
    mean, from the slots log (a sibling still open counts to t1)."""
    with tempfile.TemporaryDirectory() as t:
        log = Path(t) / "s.jsonl"
        rows = [{"event": "start", "slot": "yt1", "session": "yt_1", "t": 100.0},
                {"event": "start", "slot": "tw1", "session": "tw_1", "t": 50.0},
                {"event": "end", "slot": "tw1", "session": "tw_1", "t": 150.0},
                {"event": "start", "slot": "tt1", "session": "tt_1", "t": 120.0},
                {"event": "exit", "slot": "tt1", "rc": 0, "t": 160.0},
                {"event": "start", "slot": "tw1", "session": "tw_2", "t": 180.0}]
        log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        c = T.concurrency_over(log, "yt_1", 100.0, 200.0)
        # [100,120) 2; [120,150) 3; [150,160) 2; [160,180) 1; [180,200) 2
        assert c["steps"] == [[0.0, 2], [20.0, 3], [50.0, 2], [60.0, 1], [80.0, 2]], c
        assert c["mean"] == round((2 * 20 + 3 * 30 + 2 * 10 + 1 * 20 + 2 * 20) / 100, 4) and c["min"] == 1 and c["max"] == 3
        assert T.concurrency_over(Path(t) / "none.jsonl", "yt_1", 0, 1) is None
    src = (HERE / "exp.py").read_text()
    assert 'TRIO["concurrent"] = trio_slots.concurrency_over(slots_log, session_dir.name, t_start, None)' in src


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
        applies = [c for c in calls if c[0] == "apply"]
        last_tree = [c for c in applies if not c[1].startswith("sysctl")][-1]
        assert last_tree[1].startswith("tc qdisc del") and last_tree[2] is False         # the teardown, last
        assert [c for c in calls if c[0] == "make"] == [("make", "start-capture"), ("make", "stop-capture"),
                                                        ("make", "merge-capture")]
        for cmd, env, log in spawned:
            tag = cmd[cmd.index("--slot") + 1]
            assert T.slot_of(tag).platform == cmd[cmd.index("--platform") + 1] and "--auto" in cmd
            assert cmd[cmd.index("--link-capacity-kbit") + 1] == "104100.0"
            assert env["LVQOE_SHOT_SOURCE"] == "cdp" and env["LVDC_PREFLIGHT_JSON"].endswith(f"preflight_last_{tag}.json")
            assert env["LVDC_TRIO_RUN"] and log.name == f"{tag}_1.log"
        assert [c[2] for c in calls if c[0] == "matrix"] == [f"_trio/_summaries/coverage_last_{t}.json" for t in ("yt1", "tw1", "tt1")]
        assert all(c[3] == ("fullscreen",) for c in calls if c[0] == "matrix")          # trio v1: no fullscreen
        import record
        assert all(c[4] == tmp / "_trio" and c[5] is record.is_record for c in calls if c[0] == "matrix")   # levi: of record
        assert all(e["LVDC_TRIO_RECORD"] == "0" for _, e, _ in spawned)            # a pilot run: never of record
        assert all(c[2] == ("fullscreen",) and c[3] == tmp / "_trio" for c in calls if c[0] == "pick")
        assert not (tmp / "_summaries").exists()                 # no write into single mode's paths
        assert {c[2] for c in calls if c[0] == "accepted"} == {tmp / "_trio"}            # trio counts its own root only
        assert "/_trio/_summaries/trio_logs/" in str(spawned[0][2])
        ev = [json.loads(x) for x in (tmp / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
        assert ev[0]["event"] == "trio_up" and ev[-1]["event"] == "trio_down" and ev[-1]["end_rc"] == 0
        sysctl = [c[1] for c in calls if c[0] == "apply" and c[1].startswith("sysctl")]
        assert sysctl == ["sysctl -w net.netfilter.nf_conntrack_udp_timeout_stream=3600",
                          "sysctl -w net.netfilter.nf_conntrack_udp_timeout_stream=120"], sysctl   # set, then restored
        assert ev[0]["conntrack_timeouts_before"] == {"nf_conntrack_udp_timeout_stream": 120}
        assert sorted(e["slot"] for e in ev if e["event"] == "exit") == ["tt1", "tw1", "yt1"]

        # rolling: no max -> a slot is refilled when its session ends, until drained
        spawned.clear()
        rc, _ = _trio(tmp, {"twitch": 5}, spawn, max_per_slot=None, drain_after=6)
        assert rc == 0 and len(spawned) >= 2 and all("--slot" in c for c, _, _ in spawned)
        assert not (tmp / T.TRIO_DIR / O.TRIO_DRAIN_FILE).exists()
        assert list((tmp / T.TRIO_DIR / "_summaries").glob("trio_drain.done_*"))

        # t55: the capture offloads refused: nothing starts, EXIT_OFFLOADS, before any tree command
        spawned.clear()
        rc, calls = _trio(tmp, {"youtube": 5}, spawn,
                          offloads=lambda iface: {"iface": iface, "ok": False, "after": {"generic-receive-offload": {"on": True}},
                                                  "calls": [{"feature": "gro", "rc": 1}]})
        assert rc == O.EXIT_OFFLOADS and not spawned and not [c for c in calls if c[0] == "apply"], calls

        # P3: the default capture IFBs' offloads refused (the interface's fine): no session, EXIT_OFFLOADS, torn down
        spawned.clear()
        rc, calls = _trio(tmp, {"youtube": 5}, spawn,
                          offloads=lambda d: {"iface": d, "ok": not d.startswith("lv"), "after": {}, "calls": []})
        assert rc == O.EXIT_OFFLOADS and not spawned, rc
        assert [c for c in calls if c[0] == "apply" and not c[1].startswith("sysctl")][-1][1].startswith("tc qdisc del")

        # low disk: nothing starts, the trio ends with EXIT_LOW_DISK, the tree still comes down
        spawned.clear()
        rc, calls = _trio(tmp, {"youtube": 5}, spawn, disk_gb=O.MIN_FREE_GB - 1)
        assert rc == O.EXIT_LOW_DISK and not spawned
        assert [c for c in calls if c[0] == "apply" and not c[1].startswith("sysctl")][-1][1].startswith("tc qdisc del")

        # preflight failure: nothing starts, the trio ends with EXIT_PREFLIGHT, the tree still comes down
        spawned.clear()
        rc, calls = _trio(tmp, {"youtube": 5}, spawn, pf_ok=False)
        assert rc == O.EXIT_PREFLIGHT and not spawned
        assert [c for c in calls if c[0] == "apply" and not c[1].startswith("sysctl")][-1][1].startswith("tc qdisc del")

        # the "fb" layout (design §4): the framebuffer grows to three widths for the run and is restored; X11 shots
        fbs = []

        def xr(fb, grow=True):
            fbs.append(fb)
            if fb is None:
                return (1920, 1080)
            return tuple(int(x) for x in fb.split("x")) if grow else (1920, 1080)
        spawned.clear()
        rc, _ = _trio(tmp, {"youtube": 5, "tiktok": 5}, spawn, layout="fb", xrandr=xr)
        assert rc == 0 and fbs == [None, "5760x1080", "1920x1080"], fbs
        assert all(e["LVQOE_SHOT_SOURCE"] == "x11" and e["LVDC_TRIO_LAYOUT"] == "fb" and e["LVDC_TRIO_SCREEN"] == "1920x1080"
                   for _, e, _ in spawned)
        lay = {T.slot_of(c[c.index("--slot") + 1]).tag: T.layout_of(T.slot_of(c[c.index("--slot") + 1]), e)
               for c, e, _ in spawned}
        assert lay["yt1"]["origin"] == [0, 0] and lay["tt1"]["origin"] == [3840, 0] and lay["tt1"]["x_screen_px"] == [5760, 1080]
        assert T.layout_of(T.SLOTS["tw1"], {}) == {"layout": "cdp"}
        # a recorded round pin (levi 00261): the twitch slot's category / channel / state / geometry, in meta via env;
        # another slot unpinned; and am. 15 (5)'s band start applies in trio as in single mode
        spawned.clear()
        plat, pin = O.parse_pin("twitch:category=Chess,channel=gothamchess,state=10,geometry=window:1920x1080",
                                "levi 00261 coverage: 60 fps tier")
        O.TRIO_PINS[plat] = pin
        O.LAST_COVERAGE["yt1"] = {"initial_state_forced": {"state": "9"}}      # t56: keyed by slot tag
        try:
            rc, _ = _trio(tmp, {"twitch": 5, "youtube": 5}, spawn)
        finally:
            O.TRIO_PINS.clear()
            O.LAST_COVERAGE.pop("yt1", None)
        by = {c[c.index("--platform") + 1]: (c, e) for c, e, _ in spawned}
        tc, te = by["twitch"]
        assert rc == 0 and tc[tc.index("--tw-category") + 1] == "Chess" and tc[tc.index("--tw-channel") + 1] == "gothamchess"
        assert tc[tc.index("--initial-state") + 1] == "10" and tc[tc.index("--geometry") + 1] == "window:1920x1080"
        rec = json.loads(te["LVDC_PICK_PINNED"])
        assert rec["reason"] == "levi 00261 coverage: 60 fps tier" and rec["initial_state"] == "10", rec
        yc, ye = by["youtube"]
        assert "LVDC_PICK_PINNED" not in ye and yc[yc.index("--initial-state") + 1] == "9"
        for bad in ("twitch:channel=x", "twitch:state=12", "youtube:category=Chess", "twitch", "zz:state=3"):
            try:
                O.parse_pin(bad, "r")
                raise AssertionError(bad)
            except ValueError:
                pass

        # t53 (a), default off; restart §7: fullscreen is in the yt slot's drawn support only with --trio-yt-fullscreen
        # (env LVDC_TRIO_YT_FULLSCREEN to its process alone); the geometry is the per-session draw, never the target's
        # coupling (a forced fullscreen changes nothing) and the coverage rule runs uncoupled
        fs = lambda tag, g: "fullscreen:1920x1080" if tag == "yt1" else g      # a target that would have coupled
        for on in (False, True):
            spawned.clear()
            O.TRIO_YT_FULLSCREEN = on
            try:
                rc, calls = _trio(tmp, {"youtube": 5, "twitch": 5}, spawn, coupled=fs)
            finally:
                O.TRIO_YT_FULLSCREEN = False
            by = {c[c.index("--platform") + 1]: (c, e) for c, e, _ in spawned}
            yc, ye = by["youtube"]
            gd = json.loads(ye["LVDC_GEOMETRY_DRAW"])
            want = O.geometry.draw_geometry("youtube", gd["seed"], () if on else ("fullscreen",))
            assert yc[yc.index("--geometry-name") + 1] == gd["geometry"] == want["geometry"] and gd == want, (on, gd)
            assert yc[yc.index("--geometry") + 1] == gd["instruction"] == O.geometry.instruction_of("youtube", gd["geometry"])
            assert ("fullscreen:1920x1080" in gd["support"]) is on and (ye.get("LVDC_TRIO_YT_FULLSCREEN") == "1") is on
            assert gd["seed"].startswith(f"{O.SESSION_DRAW_SEED}:") and gd["seed"].endswith(":yt1:1"), gd
            assert "LVDC_TRIO_YT_FULLSCREEN" not in by["twitch"][1]
            mx = [c for c in calls if c[0] == "matrix" and c[1] == "youtube"]
            assert mx and mx[0][3] == (() if on else ("fullscreen",)) and mx[0][8] is False, mx

        # the "composite" layout (pilot-1, probe v3): windows at the origin, each slot's shots from its window's pixmap
        spawned.clear()
        rc, _ = _trio(tmp, {"twitch": 5}, spawn, layout="composite")
        assert rc == 0 and spawned and all(e["LVQOE_SHOT_SOURCE"] == "composite" and e["LVDC_TRIO_LAYOUT"] == "composite"
                                           for _, e, _ in spawned)
        assert T.layout_of(T.SLOTS["tw1"], {"LVDC_TRIO_LAYOUT": "composite"}) == {"layout": "composite"}
        assert O.TRIO_SHOT_SOURCE == {"cdp": "cdp", "fb": "x11", "composite": "composite"}
        # it did not grow (a window manager that keeps the screen): no session, EXIT_PREFLIGHT, restored, torn down
        fbs.clear()
        spawned.clear()
        rc, calls = _trio(tmp, {"youtube": 5}, spawn, layout="fb", xrandr=lambda fb: xr(fb, grow=False))
        assert rc == O.EXIT_PREFLIGHT and not spawned and fbs[-1] == "1920x1080"
        assert [c for c in calls if c[0] == "apply" and not c[1].startswith("sysctl")][-1][1].startswith("tc qdisc del")
        os.environ.pop("LVDC_TRIO_LAYOUT", None)
        os.environ.pop("LVDC_TRIO_SCREEN", None)

        # the pre-start preflight (pilot-0: no profiles/ link): nothing touched, no session
        spawned.clear()
        rc, calls = _trio(tmp, {"youtube": 5}, spawn,
                          trio_pf=lambda *a, **k: {"ok": False, "problems": ["profile_template_youtube"], "checks": {}})
        assert rc == O.EXIT_PREFLIGHT and not spawned and not [c for c in calls if c[0] in ("apply", "make", "preclean")]

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
        ev = [json.loads(x) for x in (tmp / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
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
