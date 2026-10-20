"""The trio's sessions of record (levi's note on 6c6ecca, 2026-10-19): the concurrent collection is the dataset of
record, so its steering (coverage floors, yields, the rotation's counts) counts only sessions of record of the current
era. A session is of record when its run was started as a counted run (orchestrate --trio-record: meta.json
concurrency.record) under ERA (concurrency.era). Pilots, the gate-2 re-run and any earlier era never count, wherever
they sit under data/_trio. The rule is this one predicate; the era changes only with an era boundary (FROZEN_COLLECTOR).
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

# t58 L-5 (levi): bumped at the N-slot adoption (t55-gro-off-2026-10-19 -> t56-nslot-2026-10-19). t56 boundary (levi
# 2026-10-20, the N_collect adoption at 5ba0b94): bumped again (t57-n5-boundary-2026-10-20). The restart (LVQoE
# restart_2026-10-21_prereg_levi.md §0, rushi GO 2026-10-20; the string fixed by levi): the record is the new era only.
# Sessions of record are the flag AND this ERA AND a collector on its counted line. levi 00138 (blocker): the era string
# alone is no line (earlier commits of the restart branch write it with other behaviour), so the counted line is the
# day-1 tip's ancestry: the boundary sha and its descendants (git merge-base --is-ancestor in this checkout; a sha the
# checkout does not know is not of record). RECORD_BOUNDARY is set in its own commit after the tip's apply gate; while
# it is None nothing is of record (fail closed). The earlier eras (c1f9181 N = 3 t55, 61a852d N = 5 t56, the t57
# boundary) are stated references, never pooled.
ERA = "t58-restart-2026-10-21"   # the era this collector writes (capture regime unchanged: GRO / GSO / TSO off)
RECORD_BOUNDARY: str | None = None
RECORD_LINES = {ERA: RECORD_BOUNDARY}          # era -> its counted line's boundary sha (None: nothing counts yet)
RECORD_ERAS = tuple(RECORD_LINES)
REPO = Path(__file__).resolve().parent.parent
STATED_ERAS = ("t56-nslot-2026-10-19",)


def counted_any_era(meta: dict | None) -> bool:
    """A counted run's session (the record flag) of any era, stated runs included: channel identity (the yt LRU / cap,
    the twitch / tt caps) spans every era, because identity is content, not record (restart prereg §0)."""
    return bool(((meta or {}).get("concurrency") or {}).get("record"))


def of_record_any_era(meta: dict | None) -> bool:
    """A counted run's session of any era but a stated run's (STATED_ERAS): rotation hygiene (not_usable) is steering
    memory about content, so it carries across an ERA bump (levi / supervisor on 5374e08), never from a stated run
    (levi 2026-10-20); dataset and steering counts use is_record."""
    c = (meta or {}).get("concurrency") or {}
    return bool(c.get("record")) and c.get("era") not in STATED_ERAS


@functools.lru_cache(maxsize=4096)
def descends(sha: str, boundary: str | None) -> bool:
    """sha is the boundary or a descendant of it in this checkout (git merge-base --is-ancestor); False for no
    boundary, no sha, or a sha / boundary the checkout does not know (fail closed)."""
    if not sha or not boundary:
        return False
    try:
        r = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", boundary, sha],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def is_record(meta: dict | None) -> bool:
    """The record flag, an era in RECORD_LINES and the session's collector sha on the era's counted line: the boundary
    or a descendant of it (descends)."""
    c = (meta or {}).get("concurrency") or {}
    if not c.get("record") or c.get("era") not in RECORD_LINES:
        return False
    sha = str(((meta or {}).get("lvdc_commit") or {}).get("sha") or "")
    return descends(sha, RECORD_LINES[c["era"]])
