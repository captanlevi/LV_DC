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


TEMPLATE_NOTE = "TEMPLATE_NOTE.txt"


def template_note_of(template: Path | None) -> str | None:
    """The template's TEMPLATE_NOTE.txt (the operator's rebuild record, e.g. tt's HEVC template, 2026-10-18; read into
    meta.json template_note so the regime boundary is in the session record; never anything else from the profile,
    which holds login cookies), at most 4 KB."""
    try:
        return (Path(template) / TEMPLATE_NOTE).read_text(errors="replace")[:4096] if template else None
    except OSError:
        return None
