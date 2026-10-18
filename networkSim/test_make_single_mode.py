#!/usr/bin/env python3
"""t53: single mode's Makefile commands are byte-identical with --concurrent off (the supervisor's landing condition).
`make -n` of every single-mode target, with fixed variables, against fixtures/make_single_mode.golden, which was taken
from the Makefile before the trio parameters (lvdc-v2 51007ea); the checkout path is normalised. A slot's overrides
(trio_slots.make_vars: IFB_INGRESS / IFB_EGRESS / EGRESS_CLASS / CDP_PORT / SLOT_SUFFIX / CHROME_PREFIX, and
the staged capture files / tcpdump stderr) are checked to land where meant.

    python3 test_make_single_mode.py [--write]        (exit 0 = pass)
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "fixtures" / "make_single_mode.golden"
VARS = ["SESSION_DIR=/S/yt_1", "CHROME_PROFILE=/P/prof", "CHROME_BIN=/usr/bin/google-chrome", "CAPTURE_DIR=/C",
        "VENV_DIR=/V", "MGMT_UDP_PORT=41641"]
TARGETS = ["enable", "mgmt-exclusion", "slow", "disable", "start-capture", "stop-capture", "merge-capture",
           "start_chrome", "kill_chrome", "change_perm"]


def dry(makefile: Path, extra=()) -> str:
    out = []
    for t in TARGETS:
        r = subprocess.run(["make", "-s", "-n", "-f", str(makefile), t, *VARS, *extra], cwd=HERE, capture_output=True,
                           text=True)
        out.append(f"=== {t}\n{r.stdout}{r.stderr}")
    return "".join(out).replace(str(HERE.parent), "<REPO>")


def main() -> int:
    if not shutil.which("make"):
        print("test_make_single_mode: skipped (no make)")
        return 0
    cur = dry(HERE / "Makefile")
    if "--write" in sys.argv:
        GOLDEN.write_text(cur)
        print(f"wrote {GOLDEN.name}")
        return 0
    assert cur == GOLDEN.read_text(), "single mode's make -n output changed (t53: byte-identical with the flag off)"
    import trio_slots                                    # the overrides a slot actually passes (exp.py --slot)
    slot = dry(HERE / "Makefile", trio_slots.make_vars(trio_slots.slot_of("yt"), Path("/T")))
    for want in ("dev lvi_yt parent 1: classid 1:10", "classid 1:11 htb", "parent 1:11 netem", "tcpdump -i lvo_yt",
                 "/C/lvi_yt.pid", "--remote-debugging-port=9223", "/C/chrome_yt.pid", "/C/chrome_yt.log",
                 "<REPO>/networkSim/in_cgroup.sh lvdc.slice/yt env SSLKEYLOGFILE=/S/yt_1/ssl_keys.log",
                 "[r]emote-debugging-port=9223", '> "/T/incoming.pcap" 2> "/T/tcpdump_in.err"',
                 '> "/T/outgoing.pcap" 2> "/T/tcpdump_out.err"', "mergecap -w /S/yt_1/merged.pcap /T/incoming.pcap "
                 "/T/outgoing.pcap", "rm /T/incoming.pcap"):
        assert want in slot, want
    assert "9222" not in slot and "ifb0" not in slot and "chrome.pid" not in slot
    print("test_make_single_mode: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
