"""The trio's sessions of record (levi's note on 6c6ecca, 2026-10-19): the concurrent collection is the dataset of
record, so its steering (coverage floors, yields, the rotation's counts) counts only sessions of record of the current
era. A session is of record when its run was started as a counted run (orchestrate --trio-record: meta.json
concurrency.record) under ERA (concurrency.era). Pilots, the gate-2 re-run and any earlier era never count, wherever
they sit under data/_trio. The rule is this one predicate; the era changes only with an era boundary (FROZEN_COLLECTOR).
"""
from __future__ import annotations

# t58 L-5 (levi): bumped at the N-slot adoption (t55-gro-off-2026-10-19 -> t56-nslot-2026-10-19). t56 boundary (levi
# 2026-10-20, the N_collect adoption at 5ba0b94: schedule families, player-error recovery, the in-stall press removed):
# bumped again so the count restart is real. The t56-nslot era (the 61a852d rounds 1-3) is a stated run (the final
# checkpoint's re-spec): never of record, for floors, targets, steering or not_usable. Steering counts the record eras:
# the N = 3 counted line at c1f9181 (t55-gro-off-2026-10-19; pooling condition iii) and this era from 5ba0b94 on; if
# pooling (iii) fails, coverage is re-read on N_collect alone at the final checkpoint.
ERA = "t57-n5-boundary-2026-10-20"   # the era this collector writes (capture regime unchanged: GRO / GSO / TSO off)
RECORD_ERAS = ("t55-gro-off-2026-10-19", ERA)
STATED_ERAS = ("t56-nslot-2026-10-19",)


def of_record_any_era(meta: dict | None) -> bool:
    """A counted run's session of any era but a stated run's (STATED_ERAS): rotation hygiene (not_usable) is steering
    memory about content, so it carries across an ERA bump (levi / supervisor on 5374e08), never from a stated run
    (levi 2026-10-20); dataset and steering counts use is_record."""
    c = (meta or {}).get("concurrency") or {}
    return bool(c.get("record")) and c.get("era") not in STATED_ERAS


def is_record(meta: dict | None) -> bool:
    c = (meta or {}).get("concurrency") or {}
    return bool(c.get("record")) and c.get("era") in RECORD_ERAS
