#!/usr/bin/env python3
"""levi 22:54 / 22:58 (memories removal): the pre-era freeze copy lives in LV_results/refs_pre_era/, outside every record
root, and only LVQoE's freeze_null names it. No LV_DC file may name it: a reader that resolved a path under it would mix
pre-era sessions into a record read. The deploy is guarded separately (S0: a grep of the deployed LV_DC trees, 0 hits).

    python3 test_refs_pre_era.py        (exit 0 = pass)
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NEEDLE = "refs_" + "pre_era"              # split, so this file does not name it
SKIP_DIRS = {".git", ".venv", "data", "current_data", "profiles", "node_modules", "__pycache__"}


def repo_files() -> list[Path]:
    """The tree's tracked files (git ls-files), else every regular file outside SKIP_DIRS, never through a symlink (data/
    resolves to the data volume)."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True).stdout
        return [REPO / p for p in out.decode().split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        files, stack = [], [REPO]
        while stack:
            d = stack.pop()
            for p in d.iterdir():
                if p.is_symlink():
                    continue
                if p.is_dir():
                    if p.name not in SKIP_DIRS:
                        stack.append(p)
                elif p.is_file():
                    files.append(p)
        return files


def hits(files: list[Path]) -> list[str]:
    out = []
    for p in files:
        if p.is_symlink() or not p.is_file():
            continue
        if NEEDLE in str(p.relative_to(REPO)) or NEEDLE.encode() in p.read_bytes():
            out.append(str(p.relative_to(REPO)))
    return out


def test_no_file_names_refs_pre_era():
    files = repo_files()
    assert len(files) > 100, len(files)
    assert hits(files) == [], hits(files)


def test_detects_a_hit():
    import tempfile
    with tempfile.TemporaryDirectory(dir=REPO / "networkSim") as t:
        p = Path(t) / "x.py"
        p.write_text(f"ROOT = 'LV_results/{NEEDLE}/freeze_trio'\n")
        assert hits([p]) == [str(p.relative_to(REPO))]


def main() -> int:
    test_no_file_names_refs_pre_era()
    test_detects_a_hit()
    print("test_refs_pre_era: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
