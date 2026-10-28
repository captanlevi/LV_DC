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
# collector v3 (levi 2026-10-24 16:49, the gate list's string; a label, not a date): the era bumps to v3. t58 is archived
# as a development set (levi 16:45), never a results source; its era string stays T58_ERA (n_draw's void rule keys on it).
# The v3 counted line starts fail-closed: RECORD_BOUNDARY None (nothing of v3 is of record) until levi sets the boundary
# sha in its own commit after the v3 tip's gate, as at the restart.
T58_ERA = "t58-restart-2026-10-21"
T58_RECORD_BOUNDARY = "975a493f7959ce39ce4977f2b91b8b7c9e7e231e"   # t58's day-1 tip T (stated reference)
# levi / supervisor 20:4x (window 1, run 20261026_185406): the v3 boundary was never set, so a counted run's orchestrator
# saw no session of record (accepted 0/N, each slot's failure streak paused its platform); b06b82f4 is counted window 1's sha
V3_ERA = "v3-collector-2026-10-24"
V3_RECORD_BOUNDARY = "b06b82f4a05906f44698627ca6ec6afa2eabb2f8"   # v3's counted line (stated reference: archived, not pooled)
# era v4 (rushi 2026-10-29, kb era.v4.dataset_of_record; levi docs/v3/era_v4_levi_2026-10-29.md §1, a label, not a date):
# boundary 3's collector (the per-frame log) starts the dataset of record. RECORD_BOUNDARY is the era commit's parent
# (boundary 3's frozen 3bf05879): the v4 string exists only from the era commit on, so "v4 AND a descendant of
# 3bf05879" is the era commit's line. Deliberately not fail-closed None (v3 W1's lesson above). RECORD_LINES is v4 only:
# v3 sessions stop counting for steering, n_draw and the stratum k (all restart at 0); rotation hygiene
# (of_record_any_era) and channel identity (counted_any_era) carry over, as at the v3 bump.
ERA = "v4-collector-2026-10-29"   # the era this collector writes
RECORD_BOUNDARY: str | None = "3bf05879fbf690d741c97c1fb9ae64acadf60ad1"   # v4: boundary 3's frozen sha and every descendant
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
