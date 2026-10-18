#!/usr/bin/env python3
"""t53: single mode's Makefile commands are byte-identical with --concurrent off (the supervisor's landing condition).
`make -n` of every single-mode target, with fixed variables, against fixtures/make_single_mode.golden, which was taken
from the Makefile before the trio parameters (lvdc-v2 51007ea); the checkout path and whitespace are normalised and
the management port's source is pinned, so the golden holds on any machine (GNU make 3.81 / 4.3, with or without
tailscaled; supervisor 2026-10-19). A slot's overrides
(IFB_INGRESS / IFB_EGRESS / EGRESS_CLASS / CDP_PORT / SLOT_SUFFIX / CHROME_PREFIX) are checked to land where meant.

    python3 test_make_single_mode.py [--write]        (exit 0 = pass)
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "fixtures" / "make_single_mode.golden"
# TS_UDP_PORT= pins the management port's source: the Makefile reads tailscaled's port with `ss` (chaos: present, the
# laptop: none), which would otherwise change mgmt-exclusion's udp_port_source between machines (supervisor 2026-10-19)
VARS = ["SESSION_DIR=/S/yt_1", "CHROME_PROFILE=/P/prof", "CHROME_BIN=/usr/bin/google-chrome", "CAPTURE_DIR=/C",
        "VENV_DIR=/V", "MGMT_UDP_PORT=41641", "TS_UDP_PORT="]


def normalise(text: str) -> str:
    """Environment-independent form of `make -n` output: whitespace runs collapsed, each line stripped (GNU make 3.81
    on the laptop and 4.3 on chaos print a recipe's continuation lines with different leading tabs)."""
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()) + "\n"
TARGETS = ["enable", "mgmt-exclusion", "slow", "disable", "start-capture", "stop-capture", "merge-capture",
           "start_chrome", "kill_chrome", "change_perm"]


def dry(makefile: Path, extra=()) -> str:
    out = []
    for t in TARGETS:
        r = subprocess.run(["make", "-s", "-n", "-f", str(makefile), t, *VARS, *extra], cwd=HERE, capture_output=True,
                           text=True)
        out.append(f"=== {t}\n{r.stdout}{r.stderr}")
    return normalise("".join(out).replace(str(HERE.parent), "<REPO>"))


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
    slot = dry(HERE / "Makefile", ["IFB_INGRESS=lvi_yt", "IFB_EGRESS=lvo_yt", "EGRESS_CLASS=1:11", "CDP_PORT=9223",
                                   "SLOT_SUFFIX=_yt", "CHROME_PREFIX=sudo /x/in_cgroup.sh lvdc.slice/yt"])
    for want in ("dev lvi_yt parent 1: classid 1:10", "classid 1:11 htb", "parent 1:11 netem", "tcpdump -i lvo_yt",
                 "/C/lvi_yt.pid", "--remote-debugging-port=9223", "/C/chrome_yt.pid", "/C/chrome_yt.log",
                 "sudo /x/in_cgroup.sh lvdc.slice/yt env SSLKEYLOGFILE", "[r]emote-debugging-port=9223"):
        assert want in slot, want
    assert "9222" not in slot and "ifb0" not in slot and "chrome.pid" not in slot
    # hot-fix 4 (yt_46): fullscreen sessions only launch with --test-type (the --no-sandbox infobar hidden); every other
    # geometry's launch is unchanged; exp.py and fullscreen_check.py build the variables in one place
    import geometry
    for spec in [g for gs in geometry.GEOMETRY_CHOICES.values() for g in gs] + ["theater:1920x1080@1.5", None]:
        args = geometry.chrome_args(spec)
        assert ("--test-type" in args) == bool(spec and spec.startswith("fullscreen")), (spec, args)
    assert geometry.chrome_args("theater:1920x1080@1.5") == ["--force-device-scale-factor=1.5"]
    mv = geometry.start_chrome_make_vars("/S/yt_1", "/P/prof", None, "youtube", "fullscreen:1920x1080")
    assert mv[:2] == ["SESSION_DIR=/S/yt_1", "CHROME_PROFILE=/P/prof"] and any("--test-type" in v for v in mv), mv
    fs = subprocess.run(["make", "-s", "-n", "start_chrome", *VARS[2:], *mv], cwd=HERE, capture_output=True, text=True).stdout
    pl = subprocess.run(["make", "-s", "-n", "start_chrome", *VARS[2:],
                         *geometry.start_chrome_make_vars("/S/yt_1", "/P/prof", None, "youtube", "player:853x480")],
                        cwd=HERE, capture_output=True, text=True).stdout
    assert "--test-type" in fs and "--no-sandbox" in fs and "--test-type" not in pl
    print("test_make_single_mode: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
