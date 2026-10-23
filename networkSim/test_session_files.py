#!/usr/bin/env python3
"""brad / levi 23:10 (a v3 defect found before any counted window: A1's rVFC side stream and LVQoE's frame extractor
both wrote <session>/frames.jsonl, and ops/verify_loop.sh skips extraction when frames.jsonl exists). General guard:
- every file name the collector writes into a session dir (session_files.COLLECTOR_FILES) is disjoint from every name
  LVQoE's readers / extractors write there (READER_FILES): no name both sides write;
- every content.js side-stream buffer key has a session file in SIDE_STREAM_FILES, A1's is rvfc_frames.jsonl, and
  browser.py writes side streams only through session_files.side_file (no f"{stream}.jsonl");
- every file-name literal in the collector's code is declared: in COLLECTOR_FILES, or in NON_SESSION (paths that are
  not inside a session dir), so a new collector file cannot appear unlisted.

    python3 test_session_files.py        (exit 0 = pass)
"""
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import session_files as SF  # noqa: E402

# names in the collector's code that are not inside a session dir (cgroup / proc files, the repo's own lists, the
# trio root's _summaries logs, current_data files)
NON_SESSION = frozenset({
    "cgroup.procs", "cpu.stat", "memory.current", "check_session.py", "chrome.log", "manifest.json",
    "not_usable_cleared.json", "preflight_last.json", "trio_preflight_last.json", "restart_simulation.json",
    "schedule_occupancy.json", "walk_simulation.json", "trio_slots.jsonl", "tw_esports_skips.jsonl",
    "yt_channel_picks.jsonl", "yt_list_leaves.jsonl", "yt_live_checks.jsonl", "yt_precheck_skips.jsonl",
})


def test_disjoint():
    assert SF.collisions() == [], SF.collisions()
    shared = sorted(c for c in SF.COLLECTOR_FILES for r in SF.READER_FILES if SF.collides(c, r))
    assert set(shared) == set(SF.SHARED_BY_DESIGN), shared       # every overlap is a named, deliberate one
    assert not any(SF.collides(c, p) for c in SF.COLLECTOR_FILES for p in ("a5tt*", "a6tt*"))   # reserved prefixes
    assert "frames.jsonl" in SF.READER_FILES and "frames.jsonl" not in SF.COLLECTOR_FILES
    assert SF.collides("motion_gt_v2.json", "motion_gt*") and not SF.collides("motion.jsonl", "motion_gt*")
    # the guard bites: a collector file named like a reader output is a collision (frames.jsonl: the 23:10 defect)
    assert SF.collisions(SF.COLLECTOR_FILES | {"frames.jsonl"}) == [("frames.jsonl", "frames.jsonl")]
    assert SF.collisions(SF.COLLECTOR_FILES | {"x_buffer_est.json"}) == [("x_buffer_est.json", "*_buffer_est*")]


def test_side_streams_mapped():
    js = (HERE.parent / "labeling" / "content.js").read_text()
    m = re.search(r"const sideBuffers = \{([^}]*)\}", js)
    keys = set(re.findall(r"(\w+)\s*:", m.group(1)))
    assert keys == set(SF.SIDE_STREAM_FILES), (keys, SF.SIDE_STREAM_FILES)
    assert SF.side_file("frames") == "rvfc_frames.jsonl"
    try:
        SF.side_file("new_stream")
        raise AssertionError("an unmapped stream must raise")
    except KeyError:
        pass
    src = (HERE / "browser.py").read_text()
    assert "session_files.side_file(stream)" in src and 'f"{stream}.jsonl"' not in src


def test_every_literal_declared():
    names = {}
    for f in list(HERE.glob("*.py")) + list((HERE / "utils").glob("*.py")):
        if f.name.startswith("test_"):
            continue
        for m in re.finditer(r'/ "([A-Za-z0-9_*-]+\.[A-Za-z0-9.]+)"', f.read_text()):
            names.setdefault(m.group(1), f.name)
    undeclared = sorted((n, f) for n, f in names.items()
                        if n not in NON_SESSION and not any(fnmatch(n, p) or n == p for p in SF.COLLECTOR_FILES))
    assert not undeclared, f"declare these in session_files.COLLECTOR_FILES or NON_SESSION: {undeclared}"
    assert not (NON_SESSION & set(SF.READER_FILES))


def main() -> int:
    test_disjoint()
    test_side_streams_mapped()
    test_every_literal_declared()
    print("test_session_files: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
