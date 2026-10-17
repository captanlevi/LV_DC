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
