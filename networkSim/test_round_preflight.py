#!/usr/bin/env python3
"""ops/round_preflight.py (supervisor / levi 2026-10-19): a clean box (no shaping qdisc, no lv* link, no lvdc nft table,
no process in lvdc.slice, no Chrome with a debugging port, no tcpdump), the offloads read off and the worktree at the
expected sha; anything else means no round. A fake runner.

    python3 test_round_preflight.py        (exit 0 = pass)
"""
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "ops"))
import round_preflight as R  # noqa: E402

OFF = ("generic-receive-offload: off\ngeneric-segmentation-offload: off\ntcp-segmentation-offload: off\n"
       "rx-gro-list: off\nrx-udp-gro-forwarding: off\n")
SHA = "aaba6ae3d5" + "0" * 30


def fake(**over):
    out = {"tc": "qdisc noqueue 0: root refcnt 2\n", "ip": "1: lo: <LOOPBACK> mtu 65536\n2: wlp0s20f3: <UP> mtu 1500\n",
           "nft": "table inet filter\n", "ps": "  PID ARGS\n  1 /sbin/init\n", "ethtool": OFF, "rev-parse": SHA + "\n",
           "status": ""}
    out.update(over)

    def run(cmd, capture_output=True, text=True, timeout=None):
        key = ("tc" if cmd[0] == "tc" else "ip" if cmd[0] == "ip" else "nft" if "nft" in cmd else "ps" if cmd[0] == "ps"
               else "ethtool" if cmd[0] == "ethtool" else "rev-parse" if "rev-parse" in cmd else "status")
        return types.SimpleNamespace(returncode=0, stdout=out[key], stderr="")
    return run


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        cg = Path(d)
        r = R.check("wlp0s20f3", "aaba6ae", Path("."), run=fake(), cg_root=cg)
        assert r["ok"], r
        dirty = {"tc": "qdisc htb 1: root refcnt 2 r2q 10 default 0x30\nqdisc ingress ffff: parent ffff:fff1\n",
                 "ip": "2: wlp0s20f3: <UP> mtu 1500\n9: lvi_yt: <BROADCAST,NOARP> mtu 1500\n",
                 "nft": "table inet lvdc_trio\n",
                 "ps": "  PID ARGS\n 77 /opt/google/chrome/chrome --remote-debugging-port=9223 --x\n 78 sudo sh -c exec tcpdump -i lvi_yt\n",
                 "ethtool": OFF.replace("generic-receive-offload: off", "generic-receive-offload: on"),
                 "rev-parse": "f5da4b7b70" + "0" * 30 + "\n", "status": " M networkSim/exp.py\n"}
        for key, val in dirty.items():
            r = R.check("wlp0s20f3", "aaba6ae", Path("."), run=fake(**{key: val}), cg_root=cg)
            assert not r["ok"], (key, r)
        (cg / "lvdc.slice" / "yt").mkdir(parents=True)                 # an empty slice is clean; a process in it is not
        (cg / "lvdc.slice" / "yt" / "cgroup.procs").write_text("")
        assert R.check("wlp0s20f3", "aaba6ae", Path("."), run=fake(), cg_root=cg)["ok"]
        (cg / "lvdc.slice" / "yt" / "cgroup.procs").write_text("4242\n")
        r = R.check("wlp0s20f3", "aaba6ae", Path("."), run=fake(), cg_root=cg)
        assert not r["ok"] and r["cgroup"]["procs"] == ["4242"], r
    print("test_round_preflight: ok")
    return 0


def test_round_preflight():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
