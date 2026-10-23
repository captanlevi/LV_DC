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
    assert len(shs) >= 10, [s.name for s in shs]   # 12 -> 10: verify_loop.sh + guard_stall_contradiction.sh archived (t69 C9)
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
    # t69 C9 (levi 2026-10-23 18:03, collector v3): no verifier loop; the window post is the only trio verifier. verify_loop.sh
    # and guard_stall_contradiction.sh are archived, and nothing in start_guards or the boot / resume path launches a verifier
    env = (OPS / "env.sh").read_text()
    for name in ("verify_loop.sh", "guard_stall_contradiction.sh"):
        assert not (OPS / name).exists() and (OPS / "_archive" / name).is_file(), name
    guards = re.search(r"^start_guards\(\) \{(.*?)^\}", env, re.M | re.S).group(1)
    code = "\n".join(l for l in guards.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"verify|guard_stall|extract_frames|checkpoint_session_list", code), code
    for s in ("resume_after_login.sh", "resume_batch.sh", "run_batch.sh"):
        t = "\n".join(l for l in (OPS / s).read_text().splitlines() if not l.lstrip().startswith("#")) if (OPS / s).exists() else ""
        assert not re.search(r"verify_loop|guard_stall_contradiction|verify_session\.py", t), s
    assert not re.search(r"LVQOE_(VERIFY|FRAMES)_DIR", env)
    # t58 T-3: the single-mode wrappers refuse a trio / N-slot run (run_batch's cleanup and pause_batch's make disable
    # would take the trio's shared tree down; pause_batch's wait assumes one exp.py), and run_batch's refusal drops its
    # cleanup trap first; the apply gate pauses through pause_batch, so it refuses with it
    rb, pb = (OPS / "run_batch.sh").read_text(), (OPS / "pause_batch.sh").read_text()
    assert re.search(r'case " \$SPEC \$OPTS " in \*" --concurrent"\*\).*trap - EXIT; exit 1', rb), "run_batch refusal"
    assert rb.index("--concurrent\"*)") < rb.index("mountpoint -q")
    assert 'grep -q -- "--concurrent"' in pb and "exp.py running (a trio?)" in pb
    assert pb.index("PAUSE refused") < pb.index('rm -f "$LVDC_STATE/BATCH_GO"') < pb.rindex("make disable")
    print("test_ops: ok")
    return 0


def test_main():
    """The script's own checks under pytest too (levi t58 T-1: the apply gate runs both runners)."""
    assert main() in (0, None)


if __name__ == "__main__":
    sys.exit(main())
