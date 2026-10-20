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


def stub(v6_lines=0, findmnt="/media/u/memories", held="google-chrome-stable intel-media-va-driver-non-free libva2", timers="inactive " * 4):
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

        # the data volume's UUID (supervisor / rushi 2026-10-20): a USB disconnect, a remount-ro, an empty dir on /
        def fm(line):
            return lambda cmd, timeout=10.0: (0, line + "\n") if cmd[0] == "findmnt" else (0, "")
        good = fm("012F-AC14 rw,nosuid,nodev,relatime,uid=1000,errors=remount-ro /media/u/chaoseater")
        env = {"LVDC_DATA_ROOT_UUID": "012f-ac14"}
        r = P.check_data_uuid(data, env, good)
        ok &= check(r["ok"] and r["checked"] and r["writable"] and not list(data.iterdir()), f"expected uuid, rw, writable -> ok ({r})")
        r = P.check_data_uuid(data, {}, good)
        ok &= check(r["ok"] and not r["checked"], "no expected uuid -> not checked")
        for line, why in (("4A1B-0001 rw,relatime /media/u/memories", "another volume"),
                          ("012F-AC14 ro,relatime,errors=remount-ro /media/u/chaoseater", "remounted ro"),
                          (" rw,relatime /", "an empty mount dir on the root fs"), ("", "findmnt finds nothing")):
            r = P.check_data_uuid(data, env, fm(line))
            ok &= check(not r["ok"], f"{why} -> not ok ({r})")
        gone = Path(td) / "unplugged"                                     # the mount dir vanished: the probe cannot write
        r = P.check_data_uuid(gone, env, good)
        ok &= check(not r["ok"] and r["writable"] is False, f"probe write fails -> not ok ({r.get('write_error')})")
        pr = P.run(data, 0.0, good)
        ok &= check("data_uuid" in pr["checks"], "run() carries data_uuid")

    ok &= check(P.HELD_PACKAGES == tuple("google-chrome-stable intel-media-va-driver-non-free libva2".split()), "holds: Chrome and the VA-API driver (tt HEVC decode)")
    sh, _ = stub(held="google-chrome-stable intel-media-va-driver-non-free libva2", timers="inactive inactive inactive inactive")
    r = P.check_holds(sh)
    ok &= check(r["ok"] or not r["applicable"], f"holds intact -> ok ({r})")
    if r["applicable"]:
        sh, _ = stub(held="google-chrome-stable", timers="inactive inactive inactive inactive")
        r = P.check_holds(sh)
        ok &= check(not r["ok"] and r["held_missing"] == ["intel-media-va-driver-non-free", "libva2"],
                    "the driver unheld -> not ok")
        sh, _ = stub(held="", timers="inactive active inactive inactive")
        r = P.check_holds(sh)
        ok &= check(not r["ok"] and r["held_missing"] and r["timers_active"] == ["apt-daily.timer"],
                    "hold dropped + a timer active -> not ok")
    # rootfs floor (supervisor 2026-10-18): chaos's root fell to 3.3 GB free
    r = P.check_rootfs(0.0)
    ok &= check(r["ok"] and r["free_gb"] is not None, f"root fs above a 0 GB floor -> ok ({r['free_gb']} GB)")
    r = P.check_rootfs(1e9)
    ok &= check(not r["ok"], "root fs below the floor -> not ok")
    ok &= check(P.ROOT_MIN_FREE_GB == 5.0 or "LVDC_ROOT_MIN_FREE_GB" in __import__("os").environ, "default floor 5 GB")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def test_main():
    """The script's own checks under pytest too (levi t58 T-1: the apply gate runs both runners)."""
    assert main() in (0, None)


if __name__ == "__main__":
    sys.exit(main())
