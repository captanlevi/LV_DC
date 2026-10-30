"""profile_clone.py: the per-session Chrome profile clone's removal (supervisor 2026-10-18).

exp.py clones the platform's template profile for every session (current_data/chrome_profiles/<platform>_<session>_<t>).
The clone is ephemeral runtime state holding login cookies, not a data product: the template is the reproducible state,
and the TLS keys and logs live in the session dir. So exp.py removes its clone at session end, after check_session has
run (removal, not archiving: an archived clone would keep the cookies around; 102 clones held 13 GB on chaos's root fs).
Only a directory directly under the clones root is ever removed; a template, the root itself, or anything elsewhere
is refused.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def remove_profile_clone(clones_root: Path, prof: Path) -> bool:
    """Remove the clone `prof` (a direct child of `clones_root`); True when it is gone. Chrome has been killed by then."""
    root, p = Path(clones_root).resolve(), Path(prof).resolve()
    if p.parent != root or not p.is_dir() or p.is_symlink():
        return False
    shutil.rmtree(p, ignore_errors=True)
    return not p.exists()


# levi 09:23 (F1, boundary 3): the extension's chrome.storage.local (labels' events, the side streams incl. the frame log,
# player_stats) lives only in the clone until the export reads it. When the export failed or never ran (the browser
# died, or CDP was unreachable at stop), this directory is copied into the session dir before the clone is removed, so
# halo4 reads the LevelDB post hoc. Only the extension storage: no cookies.
EXT_STORAGE = ("Default", "Local Extension Settings")
EXT_STORAGE_COPY = "ext_storage_raw"

# t102 #7 (levi 00402): on a COMPLETE export, 1 session in F1_REF_KEEP_EVERY keeps the same raw copy as the F1 reference
# (the only after-the-fact check of F1 on good sessions): the export leaves that session's storage uncleared (the clone
# is removed after check_session anyway) and exp.py's F1 block copies it. The selection is seeded by the session id only
# (never by platform state); the rule, seed and selection are recorded in meta.ext_export.ref_keep on every complete
# export. 0 = off: 7809b21c's behaviour (the storage cleared, nothing recorded).
F1_REF_KEEP_EVERY = 20
F1_REF_SALT = "lvqoe-f1-ref:"


def f1_ref_keep(session_id: str) -> dict | None:
    """{every, rule, selected} for this session id; None when off."""
    if not F1_REF_KEEP_EVERY:
        return None
    import hashlib
    h = int(hashlib.sha256((F1_REF_SALT + str(session_id)).encode()).hexdigest()[:8], 16)
    return {"every": F1_REF_KEEP_EVERY, "rule": f"sha256('{F1_REF_SALT}' + session id)[:8] % {F1_REF_KEEP_EVERY} == 0",
            "selected": h % F1_REF_KEEP_EVERY == 0}


def keep_ext_storage(prof: Path, session_dir: Path) -> dict:
    """Copy the clone's extension storage into session_dir/ext_storage_raw; the record for meta.ext_export.raw."""
    src, dst = Path(prof).joinpath(*EXT_STORAGE), Path(session_dir) / EXT_STORAGE_COPY
    if not src.is_dir():
        return {"copied": False, "reason": "absent"}
    try:
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    except Exception as exc:
        return {"copied": False, "error": repr(exc)[:200]}
    files = [f for f in dst.rglob("*") if f.is_file()]
    return {"copied": True, "dir": EXT_STORAGE_COPY, "files": len(files), "bytes": sum(f.stat().st_size for f in files)}


TEMPLATE_NOTE = "TEMPLATE_NOTE.txt"


def template_note_of(template: Path | None) -> str | None:
    """The template's TEMPLATE_NOTE.txt (the operator's rebuild record, e.g. tt's HEVC template, 2026-10-18; read into
    meta.json template_note so the regime boundary is in the session record; never anything else from the profile,
    which holds login cookies), at most 4 KB."""
    try:
        return (Path(template) / TEMPLATE_NOTE).read_text(errors="replace")[:4096] if template else None
    except OSError:
        return None
