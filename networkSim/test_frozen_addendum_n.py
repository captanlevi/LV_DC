#!/usr/bin/env python3
"""Addendum N's stall-only frozen run, the 2026-10-18 measure reverted (levi 2026-10-19), replayed on chaos sessions
(labels.csv columns ts / type / current_time / is_ad, fixtures/frozen_*_2026-10-19.json):
  - tw_100007 (counted round 1, a Twitch reset-and-play loop of ~2 s plays): no frozen run reaches T2 (90 s), where the
    2026-10-18 measure read one of 106 s and aborted a heavy-stall session; its loops are the stall_loop_s covariate;
  - tw_3 / tw_4 (frozen for ~20 min): still one run far past twitch's T (660 s).
The T table is pinned separately (test_frozen_constants)."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as CS  # noqa: E402
import stuck as S  # noqa: E402


def _rows(name: str) -> list[dict]:
    d = json.loads((HERE / "fixtures" / f"frozen_{name}_2026-10-19.json").read_text())
    return [dict(zip(d["columns"], r)) for r in d["rows"]]


def test_tw100007_loop_is_not_frozen():
    rows = _rows("tw_100007")
    longest = max((b - a for a, b in CS.frozen_runs(rows)), default=0.0)
    assert longest < S.T2_S, longest
    assert CS.frozen_playhead_run(rows) == longest
    loops = CS.stall_loop_s(rows)
    assert loops["stall_loops"] >= 1 and loops["stall_loop_s"] >= 60, loops


def test_tw3_tw4_still_frozen():
    for name in ("tw_3", "tw_4"):
        longest = CS.frozen_playhead_run(_rows(name))
        assert longest > CS.FROZEN_PLAYHEAD_MAX_S["twitch"], (name, longest)


def test_stall_loop_definition():
    """>= 2 stalls separated only by plays < 10 s make a loop (its span: the first stall's start to the last stall's
    end); a play of >= 10 s ends it; a lone stall is no loop."""
    ev = [(0, "start"), (100, "stall"), (104, "start"), (106, "stall"), (110, "start"), (112, "stall"), (118, "start"),
          (200, "stall"), (205, "start"), (300, "end")]
    rows = [{"type": ty, "ts": str(t * 1000)} for t, ty in ev]
    assert CS.stall_loop_s(rows) == {"stall_loop_s": 18.0, "stall_loops": 1}



def test_the_freeze_tables_measure_pin():
    """levi 2026-10-20: the frozen single freeze table (LVQoE results/audit/tw_freeze_single_table_0deb04c2fee1.json,
    rules.measure_src_sha256) pins the source of check_session's frozen_runs / stall_loop_s / read_labels, hashed as
    tw_freeze_refined.table_rules hashes it; with the refs deleted, a changed source would refuse the freeze null for
    good. Any edit to these three functions fails here first."""
    import hashlib
    import inspect
    src = "".join(inspect.getsource(getattr(CS, f)) for f in ("frozen_runs", "stall_loop_s", "read_labels"))
    assert hashlib.sha256(src.encode()).hexdigest() == \
        "bd72fc6e19d4880326b12aa2be55d9c4fabb3d2319bd255673d8319119630596"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("test_frozen_addendum_n: ok")
