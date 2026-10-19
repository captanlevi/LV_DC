#!/usr/bin/env python3
"""t53 gate 1's dry-run script (ops/diag/trio_dry_run.py): the tree it builds (management bypass first, marks saved to
conntrack, ingress restore + per-mark redirect, egress classes per mark), its rate commands (single mode's slow, per
session) and teardown, and its tc stat parsers. Runs without root (command generation only).

    python3 test_trio_dry_run.py        (exit 0 = pass)
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("trio_dry_run", HERE.parent / "ops" / "diag" / "trio_dry_run.py")
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)


def main() -> int:
    up = T.tree_up_cmds("wlp0s20f3")
    j = "\n".join(up)
    assert 'socket cgroupv2 level 2 "lvdc.slice/yt1" meta mark set 0x11 ct mark set meta mark' in j
    first_ingress = next(i for i, c in enumerate(up) if "parent ffff:" in c and "filter" in c)
    assert "sport 41641" in up[first_ingress] and "action ok" in up[first_ingress]      # management bypass first
    assert any("matchall action connmark continue" in c for c in up)
    assert any("handle 0x12 fw action mirred egress redirect dev lvi_tw1" in c for c in up)
    assert any("classid 1:20 htb rate 1000mbit" in c for c in up) and any("htb default 30" in c for c in up)
    assert any("handle 0x13 fw flowid 1:13 action mirred egress mirror dev lvo_tt1" in c for c in up)
    r = T.rate_cmds("wlp0s20f3", "yt1", T.state_params("5"))
    assert r[0].startswith("tc class replace dev lvi_yt1 parent 1: classid 1:10 htb rate ") and "netem delay" in r[1]
    assert "parent 1: classid 1:11 htb rate" in r[2] and "parent 1:11 handle 110: netem" in r[3]
    down = T.tree_down_cmds("wlp0s20f3")
    assert down[0] == "tc qdisc del dev wlp0s20f3 root" and "nft delete table inet lvdc_trio" in down
    assert "ifb0" not in j + "\n".join(down) and "ifb1" not in j + "\n".join(down)      # single mode's IFBs untouched
    cls = ("class htb 1:11 parent 1: leaf 110: prio 0 rate 2Mbit ceil 2Mbit burst 1600b cburst 1600b \n"
           " Sent 12345 bytes 67 pkt (dropped 3, overlimits 5 requeues 0) \n backlog 1514b 1p requeues 0\n")
    assert T.parse_class_stats(cls) == {"1:11": {"bytes": 12345, "packets": 67, "drops": 3, "overlimits": 5,
                                                 "backlog": 1514, "qlen": 1}}
    flt = ("filter protocol all pref 10 fw chain 0 handle 0x11 \n\tAction statistics:\n\tSent 500 bytes 3 pkt "
           "(dropped 0, overlimits 0 requeues 0) \nfilter protocol all pref 50 matchall chain 0 handle 0x1 \n"
           "\tSent 1526 bytes 9 pkt (dropped 0, overlimits 0 requeues 0) \n")
    assert T.parse_ingress_filter_stats(flt) == {"0x11": 500, "default": 1526}
    # levi's gate-1 conditions: netem counters, idle ping, the check_session reference
    q = ("qdisc htb 1: root refcnt 2 r2q 10 default 0x10 direct_packets_stat 0\n Sent 9 bytes 1 pkt (dropped 0, "
         "overlimits 0 requeues 0)\n backlog 0b 0p requeues 0\nqdisc netem 8003: parent 1:10 limit 1000 delay 20ms loss 1%\n"
         " Sent 1000 bytes 10 pkt (dropped 2, overlimits 0 requeues 0)\n backlog 0b 0p requeues 0\n")
    assert T.parse_netem_stats(q) == {"1:10": {"bytes": 1000, "packets": 10, "drops": 2}}
    pg = ("64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=41.2 ms\n64 bytes from 1.1.1.1: icmp_seq=2 ttl=57 time=40.8 ms\n"
          "64 bytes from 1.1.1.1: icmp_seq=4 ttl=57 time=43.0 ms\n\n--- 1.1.1.1 ping statistics ---\n"
          "4 packets transmitted, 3 received, 25% packet loss, time 603ms\n")
    r = T.parse_ping(pg)
    assert (r["transmitted"], r["received"], r["loss_share"], r["rtt_ms_median"]) == (4, 3, 0.25, 41.2), r
    assert T.ping_cmd("1.1.1.1", 20, "0.2") == "ping -n -c 100 -i 0.2 -w 25 1.1.1.1"
    import json, tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for i, (verdict, eps) in enumerate((("PASS", [{"state": "3", "ratio": 0.9}, {"state": "7", "ratio": 0.5}]),
                                            ("PASS", [{"state": "3", "ratio": 0.7}, {"state": 3, "ratio": 0.95}]),
                                            ("FAIL", [{"state": "3", "ratio": 0.1}]))):
            (d / "youtube" / f"yt_{i}").mkdir(parents=True)
            (d / "youtube" / f"yt_{i}" / "check.json").write_text(json.dumps({"verdict": verdict, "traffic": {"episodes": eps}}))
        ref = T.check_session_reference(d, {"yt1": ["3", "7"], "tw1": ["5"]})["per_session_state"]
        assert ref["yt1"]["3"] == {"n_sessions": 2, "n_episodes": 3, "min": 0.7, "max": 0.95, "median": 0.9, "enough": False}
        assert ref["yt1"]["7"]["n_episodes"] == 1 and ref["tw1"]["5"]["n_sessions"] == 0
    r = T.per_second_ratio([(0.0, 0), (1.0, 25000), (2.0, 50000), (3.0, 100000)], 200.0)   # 200 kbit = 25 kB/s
    assert (r["n"], r["p50"], r["max"], r["mean"]) == (3, 1.0, 2.0, 1.333), r
    assert T.per_second_ratio([(0.0, 0)], 200.0) == {"n": 0}
    # a missed counter read (None) is skipped, never read as 0 (pilot-0b's -12.8 artefact)
    assert T.per_second_ratio([(0.0, 0), (1.0, None), (2.0, 50000)], 200.0)["n"] == 1
    import shlex as _sh
    cmd = T.bulk_cmd("http://x/y.iso", 60)
    assert cmd.startswith("sh -c ") and "--max-time" in _sh.split(cmd)[2] and "http://x/y.iso" in cmd
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.log").write_text("1000\n2000\n")
        (Path(d) / "b.log").write_text("")
        assert T.logged_bytes([Path(d) / "a.log", Path(d) / "b.log"]) == 3000 and T.logged_bytes([Path(d) / "b.log"]) is None
    # t55 B2 / P3 (levi 00120): the per-slot upstream load: 4 MiB HTTPS POSTs and a UDP_SEGMENT sender to the sink
    up = T.upload_cmd(T.UPLOAD_URL, Path("/o/upload_4MiB.bin"), 359)
    assert up.startswith("sh -c ") and "--data-binary @/o/upload_4MiB.bin" in up and T.UPLOAD_URL in up and "359" in up
    u = T.udpgso_cmd("192.168.4.1", 359)
    assert "setsockopt(17, 103, 1200)" in u and "4800" in u and u.endswith("359 192.168.4.1"), u
    calls = []
    saved = T.sh
    try:
        T.sh = lambda cmd, check=True, capture=False, print_only=False: calls.append(cmd) or ("1\n2\n" if "ip.len > 1500" not in cmd else "")
        assert T.oversize(Path("/o/lvo_yt1.pcap")) == {"frames": 2, "ip_over_1500": 0, "udp_over_1500": 0}
        T.sh = lambda cmd, check=True, capture=False, print_only=False: "default via 192.168.4.1 dev wlp0s20f3 proto dhcp\n"
        assert T.default_gateway() == "192.168.4.1"
    finally:
        T.sh = saved
    print("test_trio_dry_run: ok")
    return 0


def test_trio_dry_run():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
