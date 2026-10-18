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
    assert 'socket cgroupv2 level 2 "lvdc.slice/yt" meta mark set 0x11 ct mark set meta mark' in j
    first_ingress = next(i for i, c in enumerate(up) if "parent ffff:" in c and "filter" in c)
    assert "sport 41641" in up[first_ingress] and "action ok" in up[first_ingress]      # management bypass first
    assert any("matchall action connmark continue" in c for c in up)
    assert any("handle 0x12 fw action mirred egress redirect dev lvi_tw" in c for c in up)
    assert any("classid 1:20 htb rate 1000mbit" in c for c in up) and any("htb default 30" in c for c in up)
    assert any("handle 0x13 fw flowid 1:13 action mirred egress mirror dev lvo_tt" in c for c in up)
    r = T.rate_cmds("wlp0s20f3", "yt", T.state_params("5"))
    assert r[0].startswith("tc class replace dev lvi_yt parent 1: classid 1:10 htb rate ") and "netem delay" in r[1]
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
    print("test_trio_dry_run: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
