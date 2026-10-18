#!/usr/bin/env python3
"""ops/ (t43, shagun's on-box layer): every script parses, sources env.sh, and names no box-specific path outside
env.sh (paths are variables, chaos's values the defaults); nothing in ops/ edits a tracked file (meta.json's
lvdc_commit.dirty must stay false); the autostart template is filled by install_autostart.sh; the verify /
frames loop names one LVQoE worktree (LVQOE_LOOP_DIR).

    python3 test_ops.py        (exit 0 = pass)
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent / "ops"


def main() -> int:
    shs = sorted(OPS.glob("*.sh"))
    assert len(shs) >= 11, [s.name for s in shs]
    for s in shs:
        if shutil.which("bash"):
            subprocess.run(["bash", "-n", str(s)], check=True)
        text = s.read_text()
        if s.name != "env.sh":
            assert '. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"' in text, f"{s.name} does not source env.sh"
            assert not re.search(r"/home/|/media/|captanlevi|wlp0s20f3|/dev/sd", text), f"{s.name} names a box path"
            assert not re.search(r"sed -i[^|]*\b(run_batch|README|\.py)\b", text), f"{s.name} edits a tracked file"
    for p in sorted((OPS / "diag").glob("*.py")):
        assert "captanlevi" not in p.read_text() and "/media/" not in p.read_text(), p.name
    assert "@OPS@" in (OPS / "autostart" / "lvqoe-batch-resume.desktop.in").read_text()
    # the verify / frames loop runs from ONE LVQoE worktree (supervisor 2026-10-18): an autostart relaunch takes env.sh's
    # default, so no second, stale checkout variable may remain
    env, loop = (OPS / "env.sh").read_text(), (OPS / "verify_loop.sh").read_text()
    assert len(re.findall(r'^: "\$\{LVQOE_LOOP_DIR:=', env, re.M)) == 1
    assert set(re.findall(r"\$\{?(LVQOE_\w+_DIR)", loop)) == {"LVQOE_LOOP_DIR"}
    assert not re.search(r"LVQOE_(VERIFY|FRAMES)_DIR", env + loop)
    print("test_ops: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
