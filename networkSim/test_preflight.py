#!/usr/bin/env python3
"""preflight.py (supervisor 2026-10-18): each check on a stubbed command runner, no sudo or network needed, plus the
02:20 incident shape (global IPv6 back; re-apply fails to clear it -> not ok) and a clean box -> ok.

    python3 test_preflight.py        (exit 0 = pass)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight as P  # noqa: E402


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    return bool(cond)


def stub(v6_lines=0, findmnt="/media/u/memories", held="google-chrome-stable", timers="inactive " * 4):
    calls = []

    def sh(cmd, timeout=10.0):
        calls.append(cmd)
        if cmd[:3] == ["ip", "-6", "addr"]:
            return 0, "\n".join("    inet6 2403::%d/64 scope global" % i for i in range(v6_lines))
        if cmd[0] == "findmnt":
            return 0, findmnt + "\n"
        if cmd[:2] == ["apt-mark", "showhold"]:
            return 0, held
        if cmd[0] == "systemctl":
            return 0, timers
        return 0, ""
    return sh, calls


def main():
    ok = True
    with tempfile.TemporaryDirectory() as td:
        conf = Path(td) / "noipv6.conf"
        conf.write_text("net.ipv6.conf.all.disable_ipv6=1\n")
        sh, calls = stub(v6_lines=0)
        r = P.check_ipv6(sh, sysctl_file=conf, iface="wlp0s20f3")
        ok &= check(r["ok"] and r["reapplied"] and r["global_v6"] == 0, f"clean ipv6: {r}")
        ok &= check(any("net.ipv6.conf.wlp0s20f3.disable_ipv6=1" in c for c in calls), "interface key re-applied")
        sh, _ = stub(v6_lines=4)   # 02:20 shape: NetworkManager brought 4 global addresses back
        r = P.check_ipv6(sh, sysctl_file=conf)
        ok &= check(not r["ok"] and r["global_v6"] == 4, "global IPv6 present -> not ok")

        data = Path(td) / "data"
        data.mkdir()
        sh, _ = stub(findmnt="/media/u/memories")
        r = P.check_volume(data, 0.0, sh)
        ok &= check(r["ok"], f"mounted volume with space -> ok ({r['free_gb']} GB)")
        r = P.check_volume(data, 1e9, sh)
        ok &= check(not r["ok"], "below the free floor -> not ok")
        link = Path(td) / "data_link"
        link.symlink_to(data)
        sh, _ = stub(findmnt="/")
        r = P.check_volume(link, 0.0, sh)
        ok &= check(not r["ok"] and not r["on_external"], "symlinked data/ on the root fs (volume not mounted) -> not ok")

    sh, _ = stub(held="google-chrome-stable", timers="inactive inactive inactive inactive")
    r = P.check_holds(sh)
    ok &= check(r["ok"] or not r["applicable"], f"holds intact -> ok ({r})")
    if r["applicable"]:
        sh, _ = stub(held="", timers="inactive active inactive inactive")
        r = P.check_holds(sh)
        ok &= check(not r["ok"] and r["held_missing"] and r["timers_active"] == ["apt-daily.timer"],
                    "hold dropped + a timer active -> not ok")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
