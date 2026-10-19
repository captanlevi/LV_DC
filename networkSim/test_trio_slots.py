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


def test_leak_fixes():
    """pilot-0 read (levi 1a / 1b): socketless egress packets take their connection's mark from conntrack (our marks
    only); a session's mark carries its epoch, so the slot's previous session's conntrack marks miss its filters; every
    Chrome process is swept back into the slot's cgroup."""
    up = T.shared_up_cmds(IF)
    restore = [c for c in up if "lvdc restore" in c]
    assert len(restore) == 1 and "meta mark 0 ct mark & 0xf0 == 0x10 meta mark set ct mark" in restore[0]
    assert up.index(restore[0]) > up.index(next(c for c in up if "nft add chain" in c))
    yt = T.SLOTS["youtube"]
    e3 = yt.with_epoch(100003)
    assert (e3.mark, e3.egress_class, e3.prio, e3.tag) == (0x311, "1:311", 10, "yt") and e3.record()["epoch"] == 3
    assert yt.with_epoch(16).mark == 0x11 and T.SLOTS["tiktok"].with_epoch(5).mark == 0x513
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
        _proc(root, 101, 100, "chrome --type=utility --utility-sub-type=network.mojom.NetworkService", "0::/lvdc.slice/yt")
        _proc(root, 102, 100, "chrome --type=utility --utility-sub-type=audio.mojom.AudioService",
              "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-com.google.Chrome-100.scope")
        moved = []

        def mover(pid):
            moved.append(pid)
            (root / str(pid) / "cgroup").write_text("0::/lvdc.slice/yt\n")
            return True
        sw = T.CgroupSweeper(yt, lambda: 100, proc=root, mover=mover)
        assert sw.sweep() == 2 and moved == [100, 102]
        assert sw.sweep() == 0                                       # all in the slot now
        rec = sw.stop()
        assert rec["n_moves"] == 2 and rec["sweeps"] == 2 and rec["moves"][0]["from"].endswith("Chrome-100.scope")
        assert T.socket_owner(100, yt, proc=root)["all_in_slot"]
        assert T.CgroupSweeper(yt, lambda: None, proc=root, mover=mover).sweep() == 0


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
        fl = T.trio_root_floor_gb(data, 5.0)
        assert fl == {"single_floor_gb": 5.0, "largest_pcap_gb": 3.0, "floor_gb": 14.0}


def _pkt(t, src, dst, proto, sport, dport, n=100, dcid="", sni="", qname="", resp="", ans="", plen=None):
    r = dict.fromkeys(F.FIELDS, "")
    if plen is not None:
        r["tcp.len" if proto == "tcp" else "udp.length"] = str(plen if proto == "tcp" else plen + 8)
    r.update({"frame.time_epoch": str(t), "frame.len": str(n), "ip.src": src, "ip.dst": dst, "quic.dcid": dcid,
              "tls.handshake.extensions_server_name": sni, "dns.qry.name": qname, "dns.flags.response": resp,
              "dns.count.answers": ans})
    r["tcp.srcport" if proto == "tcp" else "udp.srcport"] = str(sport)
    r["tcp.dstport" if proto == "tcp" else "udp.dstport"] = str(dport)
    return r


def test_flow_overlap():
    me = "10.0.0.2"
    yt = [_pkt(100.0, me, "142.250.1.1", "udp", 50001, 443, 1300, "aabbccddeeff0011", "rr1---sn-x.googlevideo.com"),
          _pkt(101.0, "142.250.1.1", me, "udp", 443, 50001, 1300, "1122334455667788"),
          _pkt(101.5, me, "172.217.1.1", "tcp", 50002, 443, 400, sni="www.youtube.com"),
          _pkt(99.0, me, "8.8.8.8", "udp", 40000, 53, 80, qname="rr1---sn-x.googlevideo.com", resp="1", ans="1")]
    tw = [_pkt(100.5, me, "151.101.1.1", "tcp", 50100, 443, 900, sni="video-weaver.fra02.hls.ttvnw.net")]
    dflt = [_pkt(98.0, "127.0.0.53", me, "udp", 53, 41000, 90, qname="video-weaver.fra02.hls.ttvnw.net", resp="1", ans="2")]
    s = {"youtube/yt_1": {"platform": "youtube", "window": [95.0, 200.0], "s": F.summarise(yt)},
         "twitch/tw_1": {"platform": "twitch", "window": [96.0, 200.0], "s": F.summarise(tw)}}
    rep = F.report(s, F.summarise(dflt, keep_udp=True))
    assert not rep["leak"], rep
    y = rep["sessions"]["youtube/yt_1"]
    assert y["dns"]["answered_own_pcap"] == 1 and rep["sessions"]["twitch/tw_1"]["dns"]["answered_default_capture"] == 1
    assert y["quic"] == {"bytes_share": round(2600 / 3080, 4), "flows_share": round(1 / 3, 4), "quic_flows": 1, "flows": 3}
    assert y["default_udp_from_media_addresses"]["packets"] == 0 and y["default_udp_from_media_addresses"]["media_addresses"] == 1
    assert F.family("rr1---sn-x.googlevideo.com") == "youtube" and F.family("www.google.com") is None
    # a leak: the tw session also holds a yt flow, a QUIC CID and a yt SNI
    s["twitch/tw_1"]["s"] = F.summarise(tw + [_pkt(100.5, me, "142.250.1.1", "udp", 50001, 443, 1300, "aabbccddeeff0011",
                                                    "rr1---sn-x.googlevideo.com")])
    rep = F.report(s, F.summarise(dflt, keep_udp=True))
    assert rep["leak"] and rep["pairs"][0]["shared_flows"] == 1 and rep["pairs"][0]["shared_quic_cids"] == 1
    assert rep["sessions"]["twitch/tw_1"]["sibling_family_snis"] == {"youtube": ["rr1---sn-x.googlevideo.com"]}
    # L-A3: yt media (by SNI) in the default class during the yt session
    s["twitch/tw_1"]["s"] = F.summarise(tw)
    rep = F.report(s, F.summarise(dflt + [_pkt(120.0, me, "142.250.9.9", "tcp", 50200, 443, sni="rr5---sn-y.googlevideo.com")],
                                  keep_udp=True))
    assert rep["leak"] and rep["sessions"]["youtube/yt_1"]["default_media_snis_in_window"] == ["rr5---sn-y.googlevideo.com"]
    # the conntrack-timeout case: a short-header QUIC packet from the yt media address, unmarked, on a new client port
    rep = F.report(s, F.summarise(dflt + [_pkt(150.0, "142.250.1.1", me, "udp", 443, 50009, 1200, "99887766aabbccdd")],
                                  keep_udp=True))
    hit = rep["sessions"]["youtube/yt_1"]["default_udp_from_media_addresses"]
    assert rep["leak"] and hit["packets"] == 1 and hit["quic_packets"] == 1 and hit["bytes"] == 1200
    # levi's call on pilot-1, item 11: each Chrome's own mDNS on the fixed 5-tuple is reported when packet-disjoint
    mdns = lambda tt: _pkt(tt, me, "224.0.0.251", "udp", 5353, 5353, 120, plen=78)
    s = {"youtube/yt_1": {"platform": "youtube", "window": [95.0, 200.0], "s": F.summarise(yt + [mdns(110.0), mdns(130.0)])},
         "twitch/tw_1": {"platform": "twitch", "window": [96.0, 200.0], "s": F.summarise(tw + [mdns(115.0), mdns(125.0)])}}
    pr = F.report(s, None)["pairs"][0]
    assert (pr["shared_flows"], pr["shared_unicast_flows"]) == (1, 0), pr
    assert json.loads(json.dumps(pr["shared_link_local_multicast"])) == [{"flow": ["udp", ["10.0.0.2", 5353], ["224.0.0.251", 5353]],
                                                  "packets_a": 2, "packets_b": 2, "packets_in_both": 0}], pr
    s["twitch/tw_1"]["s"] = F.summarise(tw + [mdns(115.0), mdns(130.0)])    # one packet in both pcaps
    assert F.report(s, None)["pairs"][0]["shared_link_local_multicast"][0]["packets_in_both"] == 1
    # split flows: payload-carrying packets in the default class counted apart from control ones (RST / ACK)
    split = [_pkt(102.0, me, "172.217.1.1", "tcp", 50002, 443, 54, plen=0),
             _pkt(102.5, "172.217.1.1", me, "tcp", 443, 50002, 1400, plen=1346)]
    sy = F.summarise(yt, keep_pkts=True)
    sd = F.split_detail(sy, F.summarise(split, keep_pkts=True), "youtube")
    assert sd["flows"] == 1 and sd["rows"][0]["payload_packets"] == 1 and sd["rows"][0]["default_packets"] == 2, sd
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "nf_conntrack_udp_timeout").write_text("30\n")
        (Path(d) / "nf_conntrack_udp_timeout_stream").write_text("120\n")
        assert T.conntrack_timeouts(Path(d)) == {"nf_conntrack_udp_timeout": 30, "nf_conntrack_udp_timeout_stream": 120,
                                                 "nf_conntrack_tcp_timeout_established": None}


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


def test_split_detail():
    """Supervisor (i): a flow split between a session and the default class, by when its default packets came."""
    me, cdn = "10.0.0.2", "142.250.1.1"
    s_rows = [_pkt(100.0 + i, me, cdn, "udp", 50001, 443, 1200, sni="rr1---sn-x.googlevideo.com" if i == 0 else "")
              for i in range(10)]                                                    # flow A: 100..109
    s_rows += [_pkt(t, me, cdn, "udp", 50002, 443, 1200) for t in (100.0, 105.0, 200.0, 201.0)]   # flow B: a 95 s gap
    s_rows += [_pkt(t, me, cdn, "tcp", 50003, 443, 1500) for t in (100.0, 101.0, 102.0)]           # flow C
    s_rows += [_pkt(t, me, cdn, "tcp", 50004, 443, 1500) for t in (100.0, 110.0)]                  # flow D
    d_rows = [_pkt(115.0, me, cdn, "udp", 50001, 443, 66),                                         # A: after its end
              _pkt(180.0, cdn, me, "udp", 443, 50002, 1200),                                       # B: after the idle gap
              _pkt(101.5, cdn, me, "tcp", 443, 50003, 1500),                                       # C: mid-flow
              _pkt(90.0, me, cdn, "tcp", 50004, 443, 74)]                                          # D: before its first
    d_rows[0]["tcp.flags.str"] = ""
    d_rows[2]["tcp.flags.str"] = "·······A····"
    default = F.summarise(d_rows, keep_udp=True, keep_pkts=True)
    s = F.summarise(s_rows, keep_pkts=set(default["flows"]))
    det = F.split_detail(s, default, "youtube")
    assert det["by_class"] == {"after_last": 1, "after_idle": 1, "mid_flow": 1, "before_first": 1}, det["by_class"]
    assert det["media_flows_by_class"] == {"after_last": 1}
    assert det["default_packets_by_direction"] == {"up": 2, "down": 2}
    rows = {r["flow"][1][1] if r["flow"][1][0] == me else r["flow"][2][1]: r for r in det["rows"]}
    assert rows[50002]["max_gap_before_s"] == 75.0 and rows[50003]["tcp_flags"] == {"·······A····": 1}
    # levi (round 1): a link-local multicast flow (mDNS 5353 -> 224.0.0.251:5353, a host-wide 5-tuple) carries
    # packets_in_both: its default packets matching a session packet by (time, length); avahi's own query matches none
    mrow = [_pkt(t, me, "224.0.0.251", "udp", 5353, 5353, 82) for t in (100.0, 101.0)]
    dm = [_pkt(150.0, me, "224.0.0.251", "udp", 5353, 5353, 87), _pkt(101.0, me, "224.0.0.251", "udp", 5353, 5353, 82)]
    dflt = F.summarise(dm, keep_udp=True, keep_pkts=True)
    sm = F.summarise(mrow, keep_pkts=set(dflt["flows"]))
    r = F.split_detail(sm, dflt, "youtube")["rows"][0]
    assert r["link_local_multicast"] is True and r["packets_in_both"] == 1 and r["default_packets"] == 2, r
    assert "link_local_multicast" not in F.split_detail(s, default, "youtube")["rows"][0]


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
          xrandr=None, trio_pf=None, disk_gb=1000.0, offloads=None):
    calls = []
    saved = {k: getattr(O, k) for k in ("_RUNS_DIR", "_pick", "session_matrix", "accepted_sessions", "report_flags",
                                        "_summary", "preclean", "most_needed_state", "coupled_geometry", "free_gb")}
    O.free_gb = lambda path: disk_gb            # the test's disk, not the machine's (flaky at 23 GB free on the laptop)
    saved_pf, saved_nft, saved_stage, saved_tpf = O.preflight.run, T.nft_handle_of, T.STAGING, T.trio_preflight
    saved_ct = T.conntrack_timeouts
    T.conntrack_timeouts = lambda proc=None: {"nf_conntrack_udp_timeout_stream": 120}
    T.trio_preflight = trio_pf or (lambda *a, **k: {"ok": True, "problems": [], "checks": {}})
    O._RUNS_DIR = tmp
    O._pick = lambda p, nl, now, rg, ex=(), root=None: calls.append(("pick", p, ex, root)) and None or (None, None, None)
    O.session_matrix = lambda p, f=None, ex=(), data_dir=None, select=None: calls.append(("matrix", p, f, ex, data_dir, select)) or "A"
    O.accepted_sessions = lambda p, root=None: calls.append(("accepted", p, root)) and 0 or 0
    O.report_flags = lambda p, t, root=None: []
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
            (tmp / T.TRIO_DIR / O.TRIO_DRAIN_FILE).write_text("")
        assert n["sleeps"] < 200, "run_trio did not end"
    try:
        rc = O.run_trio(targets, "A", iface=IF, capacity_kbit=104100.0, max_per_slot=max_per_slot, stagger_s=0,
                        spawn=spawn, apply=lambda cmds, check=True: calls.append(("apply", cmds[0], check)) or
                        [{"cmd": c, "rc": 0} for c in cmds],
                        make=lambda args: calls.append(("make", args[0])) or 0, sleep=sleep, layout=layout,
                        xrandr=xrandr, offloads=offloads or (lambda iface: {"iface": iface, "ok": True, "after": {}, "calls": []}))
    finally:
        for k, v in saved.items():
            setattr(O, k, v)
        O.preflight.run, T.nft_handle_of, T.STAGING, T.trio_preflight = saved_pf, saved_nft, saved_stage, saved_tpf
        T.conntrack_timeouts = saved_ct
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
        assert [c[2] for c in calls if c[0] == "matrix"] == [f"_trio/_summaries/coverage_last_{t}.json" for t in ("yt", "tw", "tt")]
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
        assert sorted(e["slot"] for e in ev if e["event"] == "exit") == ["tt", "tw", "yt"]

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
        assert lay["yt"]["origin"] == [0, 0] and lay["tt"]["origin"] == [3840, 0] and lay["tt"]["x_screen_px"] == [5760, 1080]
        assert T.layout_of(T.SLOTS["twitch"], {}) == {"layout": "cdp"}
        # a recorded round pin (levi 00261): the twitch slot's category / channel / state / geometry, in meta via env;
        # another slot unpinned; and am. 15 (5)'s band start applies in trio as in single mode
        spawned.clear()
        plat, pin = O.parse_pin("twitch:category=Chess,channel=gothamchess,state=10,geometry=window:1920x1080",
                                "levi 00261 coverage: 60 fps tier")
        O.TRIO_PINS[plat] = pin
        O.LAST_COVERAGE["youtube"] = {"initial_state_forced": {"state": "9"}}
        try:
            rc, _ = _trio(tmp, {"twitch": 5, "youtube": 5}, spawn)
        finally:
            O.TRIO_PINS.clear()
            O.LAST_COVERAGE.pop("youtube", None)
        by = {c[c.index("--platform") + 1]: (c, e) for c, e, _ in spawned}
        tc, te = by["twitch"]
        assert rc == 0 and tc[tc.index("--tw-category") + 1] == "Chess" and tc[tc.index("--tw-channel") + 1] == "gothamchess"
        assert tc[tc.index("--initial-state") + 1] == "10" and tc[tc.index("--geometry") + 1] == "window:1920x1080"
        rec = json.loads(te["LVDC_PICK_PINNED"])
        assert rec["reason"] == "levi 00261 coverage: 60 fps tier" and rec["initial_state"] == "10", rec
        yc, ye = by["youtube"]
        assert "LVDC_PICK_PINNED" not in ye and yc[yc.index("--initial-state") + 1] == "9"
        for bad in ("twitch:channel=x", "twitch:state=11", "youtube:category=Chess", "twitch", "zz:state=3"):
            try:
                O.parse_pin(bad, "r")
                raise AssertionError(bad)
            except ValueError:
                pass

        # the "composite" layout (pilot-1, probe v3): windows at the origin, each slot's shots from its window's pixmap
        spawned.clear()
        rc, _ = _trio(tmp, {"twitch": 5}, spawn, layout="composite")
        assert rc == 0 and spawned and all(e["LVQOE_SHOT_SOURCE"] == "composite" and e["LVDC_TRIO_LAYOUT"] == "composite"
                                           for _, e, _ in spawned)
        assert T.layout_of(T.SLOTS["twitch"], {"LVDC_TRIO_LAYOUT": "composite"}) == {"layout": "composite"}
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
