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


def record_root_check(root: Path | None = None, uuid_of=None) -> dict:
    """{marker, uuid_ok, is_record, reason, resolved} (brad / levi 18:56, fail closed both ways): `marker` = the root
    holds RECORD_MARKER at all; `uuid_ok` = its UUID equals the volume's under it (None when either is unreadable);
    is_record = marker and uuid_ok is True. A counted run needs is_record; a non-record run is refused on any marker."""
    import json
    res = Path(root or DATA_ROOT).resolve()
    m = res / RECORD_MARKER
    out = {"marker": m.is_file(), "uuid_ok": None, "is_record": False, "resolved": str(res)}
    if not out["marker"]:
        return {**out, "reason": "no marker"}
    try:
        want = (json.loads(m.read_text()) or {}).get("uuid")
    except (OSError, ValueError, AttributeError):
        return {**out, "reason": "marker unreadable"}
    have = (uuid_of or volume_uuid)(res)       # looked up at call time (tests replace volume_uuid)
    if have is None or not want:
        return {**out, "reason": "marker present, volume UUID unreadable or marker without uuid"}
    if want != have:
        return {**out, "uuid_ok": False, "reason": f"marker UUID {want} is not the volume's {have}"}
    return {**out, "uuid_ok": True, "is_record": True, "reason": "marker + volume UUID"}


def kind_refusal(of_record: bool, root: Path | None = None, uuid_of=None) -> str | None:
    """levi 18:45 / 18:56 (C8, fail closed both ways, every mode): a counted run needs the verified record root (the
    marker and its volume's UUID); a run that is not of record never writes under a marked root, UUID readable or not."""
    chk = record_root_check(root, uuid_of)
    if of_record and not chk["is_record"]:
        return (f"a counted run on a data root that is not the verified record root ({chk['resolved']}: {chk['reason']}); "
                "refused (C8: the marker and its volume's UUID)")
    if not of_record and chk["marker"]:
        return (f"a run that is not of record on a marked record root ({chk['resolved']}: {chk['reason']}); refused (C8: "
                "every non-record run goes to a dry root)")
    return None


def path_kind_refusal(path: Path, of_record: bool, uuid_of=None) -> str | None:
    """brad / levi 19:00: exp.py's wall in every branch (--auto, --save-dir, current_data), before any dir is made: the
    nearest ancestor of `path` (itself included) holding RECORD_MARKER decides. Under one, only a counted slot run on
    the verified record root passes; under none, a counted run is refused (it must write to the record root)."""
    p = Path(path).resolve()
    for a in (p, *p.parents):
        if (a / RECORD_MARKER).is_file():
            chk = record_root_check(a, uuid_of)
            if of_record and chk["is_record"]:
                return None
            return (f"{'a counted run on an unverified' if of_record else 'a run that is not of record under a marked'} "
                    f"record root ({a}: {chk['reason']}); refused (C8: every non-record run goes to a dry root)")
    if of_record:
        return f"a counted run outside the record root ({p}: no marker above it); refused (C8)"
    return None
