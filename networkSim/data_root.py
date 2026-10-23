"""The collection's data root: one setting, recorded per session (supervisor 2026-10-20, before the 4 TB disk).

DATA_ROOT is LVDC_DATA_ROOT when set, else the repo's data/ (on chaos a symlink to the data volume: the setting every
process sees, ops' scripts and manual runs alike). Every module that places or counts sessions reads it here (exp.py,
orchestrate, trio_slots, coverage_report, balance, batch_summary, preflight). The trio's staging (trio_slots.STAGING)
and the trio floor (trio_slots.staging_floor_gb, preflight's volume check) follow it. exp.py records record() in
meta.json capture.data_root, so the volume each session was written to is on record. A migration to another volume is a
sha256-verified move plus this setting (repoint data/, or set LVDC_DATA_ROOT everywhere, with ops/env.sh's LVDC_MOUNT),
never a collector behaviour change (FROZEN_COLLECTOR's storage note).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_DATA = Path(__file__).resolve().parent.parent / "data"
DATA_ROOT = Path(os.environ.get("LVDC_DATA_ROOT") or REPO_DATA)


def record(root: Path | None = None) -> dict:
    """The data root as set, how, what it resolves to and the volume under it (findmnt: mount point, source, fs)."""
    root = Path(root or DATA_ROOT)
    out = {"set": str(root), "source": "LVDC_DATA_ROOT" if os.environ.get("LVDC_DATA_ROOT") else "repo data/",
           "resolved": str(root.resolve())}
    try:
        r = subprocess.run(["findmnt", "-no", "TARGET,SOURCE,FSTYPE", "--target", out["resolved"]],
                           capture_output=True, text=True, timeout=5)
        parts = r.stdout.split()
        if r.returncode == 0 and len(parts) >= 3:
            out.update(mount=parts[0], device=parts[1], fstype=parts[2])
    except (OSError, subprocess.SubprocessError):
        pass
    return out


# t67 C8 (levi 18:45, collector v3): every non-record run goes to a dry root, and a counted run only to the record root.
# The record root is the data root holding RECORD_MARKER ({"uuid": <the volume's filesystem UUID>}, written once by ops on
# the record volume); a marker whose UUID is not the volume's under it (a copied marker) does not count.
RECORD_MARKER = ".lvdc_record_root"


def volume_uuid(path: Path, run=subprocess.run) -> str | None:
    try:
        r = run(["findmnt", "-no", "UUID", "--target", str(path)], capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def record_root_check(root: Path | None = None, uuid_of=volume_uuid) -> dict:
    """{is_record, reason, resolved}: the data root is the record root iff it holds RECORD_MARKER with the UUID of the
    volume under it (when the volume's UUID can be read; else the marker alone, stated)."""
    import json
    res = Path(root or DATA_ROOT).resolve()
    m = res / RECORD_MARKER
    if not m.is_file():
        return {"is_record": False, "reason": "no marker", "resolved": str(res)}
    try:
        want = (json.loads(m.read_text()) or {}).get("uuid")
    except (OSError, ValueError, AttributeError):
        return {"is_record": False, "reason": "marker unreadable", "resolved": str(res)}
    have = uuid_of(res)
    if have is None:
        return {"is_record": True, "reason": "marker (volume UUID unreadable)", "resolved": str(res)}
    if want != have:
        return {"is_record": False, "reason": f"marker UUID {want} is not the volume's {have}", "resolved": str(res)}
    return {"is_record": True, "reason": "marker + volume UUID", "resolved": str(res)}
