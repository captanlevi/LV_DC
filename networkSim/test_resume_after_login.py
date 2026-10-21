"""ops/resume_after_login.sh with stub binaries on PATH (levi 02:38, a named defect: data-volume writes before NTP, a
launch path that ignored RESUME_HOLD): synced -> guards and the launch; never synced -> neither, fail closed, nothing
under the data volume; synced after a few polls -> proceeds; RESUME_HOLD -> no launch. No real guard, mount or batch
runs: setsid / sudo / systemctl and the rest are stubs that only record their calls."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "ops" / "resume_after_login.sh"

STUBS = {
    "timedatectl": 'n=$(cat "$STUB/ntp_calls" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$STUB/ntp_calls"\n'
                   '[ "$n" -ge "${NTP_YES_AT:-1}" ] && [ "${NTP_YES_AT:-1}" != never ] && echo yes || echo no',
    "findmnt": 'echo "$LVDC_MOUNT"',
    "mountpoint": "exit 0",
    "sudo": 'echo "sudo $*" >> "$STUB/calls"',
    "sysctl": "echo 1",
    "ip": "exit 0",
    "systemctl": 'echo "systemctl $*" >> "$STUB/calls"; echo inactive',
    "pactl": "exit 0",
    "pgrep": "exit 1",
    "setsid": 'echo "setsid $*" >> "$STUB/calls"',
    "nohup": 'echo "nohup $*" >> "$STUB/calls"',
}


def run(tmp: Path, ntp_yes_at="1", hold=False, batch_go=True, wait_s="2"):
    stub, state, mount = tmp / "stub", tmp / "state", tmp / "mount"
    for d in (stub, state, mount / "LV_DC_v2_data"):
        d.mkdir(parents=True, exist_ok=True)
    for name, body in STUBS.items():
        p = stub / name
        p.write_text("#!/bin/bash\n" + body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    if batch_go:
        (state / "BATCH_GO").write_text("")
    if hold:
        (state / "RESUME_HOLD").write_text("concurrent mode (test)")
    env = {"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(tmp), "STUB": str(stub), "NTP_YES_AT": ntp_yes_at,
           "LVDC_STATE": str(state), "LVDC_MOUNT": str(mount), "LVDC_DATA": str(mount / "LV_DC_v2_data"),
           "LVDC_DEVICE": "/dev/null", "LVDC_UID": str(os.getuid()), "LVDC_NTP_WAIT_S": wait_s, "LVDC_NTP_POLL_S": "0.2",
           "OPS": str(HOOK.parent)}
    before = {p for p in (mount).rglob("*")}
    r = subprocess.run(["bash", str(HOOK)], env=env, capture_output=True, text=True, timeout=60)
    log = "\n".join(p.read_text() for p in sorted(state.glob("resume_*.log")))
    calls = (stub / "calls").read_text() if (stub / "calls").exists() else ""
    written = {p for p in mount.rglob("*")} - before
    return r.returncode, log, calls, written


def _guards(calls):
    return "guard_stall_contradiction.sh" in calls or "ipv6_watchdog.sh" in calls


def test_synced_starts_guards_and_launch():
    with tempfile.TemporaryDirectory() as d:
        rc, log, calls, written = run(Path(d))
        assert rc == 0 and "NTP synchronized=yes" in log and _guards(calls) and "run_batch.sh" in calls, (rc, log, calls)
        assert "RESUME: batch wrapper relaunched" in log


def test_never_synced_fails_closed():
    with tempfile.TemporaryDirectory() as d:
        rc, log, calls, written = run(Path(d), ntp_yes_at="never")
        assert rc != 0 and "ABORT: NTP not synced" in log, (rc, log)
        assert not _guards(calls) and "run_batch.sh" not in calls, calls
        assert not written, written                                   # nothing on the data volume
        assert "ipv6_watchdog" in log and "guard_stall_contradiction" in log   # the withheld guards are named
        assert "disable_ipv6" in calls or "sysctl" in calls                    # IPv6-off still applied


def test_synced_after_polls():
    with tempfile.TemporaryDirectory() as d:
        rc, log, calls, _ = run(Path(d), ntp_yes_at="4", wait_s="5")
        assert rc == 0 and "NTP synchronized=yes" in log and "run_batch.sh" in calls, (rc, log)
        assert int((Path(d) / "stub" / "ntp_calls").read_text()) >= 4


def test_resume_hold_blocks_the_launch():
    with tempfile.TemporaryDirectory() as d:
        rc, log, calls, _ = run(Path(d), hold=True)
        assert rc == 0 and _guards(calls) and "run_batch.sh" not in calls and "no resume (RESUME_HOLD" in log, (log, calls)
    with tempfile.TemporaryDirectory() as d:
        rc, log, calls, _ = run(Path(d), batch_go=False)
        assert rc == 0 and "run_batch.sh" not in calls and "BATCH_GO=no" in log


def main() -> int:
    test_synced_starts_guards_and_launch()
    test_never_synced_fails_closed()
    test_synced_after_polls()
    test_resume_hold_blocks_the_launch()
    print("test_resume_after_login: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
