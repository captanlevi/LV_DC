"""The trio's sessions of record (levi's note on 6c6ecca, 2026-10-19): the concurrent collection is the dataset of
record, so its steering (coverage floors, yields, the rotation's counts) counts only sessions of record of the current
era. A session is of record when its run was started as a counted run (orchestrate --trio-record: meta.json
concurrency.record) under ERA (concurrency.era). Pilots, the gate-2 re-run and any earlier era never count, wherever
they sit under data/_trio. The rule is this one predicate; the era changes only with an era boundary (FROZEN_COLLECTOR).
"""
from __future__ import annotations

# t58 L-5 (levi): bumped at the N-slot adoption, the count boundary it restarts; earlier eras (t55-gro-off-2026-10-19:
# the trio counted rounds at c1f9181) and every pilot are not of record for this era's steering, targets or not_usable
ERA = "t56-nslot-2026-10-19"         # the capture regime of the counted collection (GRO / GSO / TSO off), N-slot


def is_record(meta: dict | None) -> bool:
    c = (meta or {}).get("concurrency") or {}
    return bool(c.get("record")) and c.get("era") == ERA
