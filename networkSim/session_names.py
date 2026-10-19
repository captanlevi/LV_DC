"""session_names.py: a session's directory name, <abbrev>_<n>, from a monotonic per-platform counter (supervisor
2026-10-18: exp.py took the lowest free number across data/<p> and data/_failed/<p>, so a removed session's name was
reused, e.g. twitch/tw_1). The counter lives at data/_summaries/session_counter_<platform>.json and never decreases.
On its first use it starts at the highest number among the existing names (accepted and _failed); a name that is
still present (a tombstone, say) is skipped either way.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


def counter_path(data_dir: Path, platform: str) -> Path:
    return Path(data_dir) / "_summaries" / f"session_counter_{platform}.json"


def _highest_existing(data_dir: Path, platform: str, abbrev: str) -> int:
    pat = re.compile(rf"{re.escape(abbrev)}_(\d+)$")
    ns = [int(m.group(1)) for d in (Path(data_dir) / platform, Path(data_dir) / "_failed" / platform) if d.exists()
          for p in d.iterdir() if (m := pat.match(p.name))]
    return max(ns, default=0)


def next_session_dir(data_dir: Path, platform: str, abbrev: str, floor: int = 0) -> Path:
    """Create and return data/<platform>/<abbrev>_<n> with n above every name ever handed out; persist n. floor (t53
    trio: data_dir = data/_trio, its own counter): n above it too, so trio names never meet single mode's."""
    import fcntl
    data_dir = Path(data_dir)
    cp = counter_path(data_dir, platform)
    cp.parent.mkdir(parents=True, exist_ok=True)
    # t56: two slots of one platform start their sessions concurrently: the counter is read, advanced and the dir made
    # under an exclusive lock, and the dir is created exclusively (a name is handed out once)
    with open(cp.with_suffix(".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            last = int(json.loads(cp.read_text())["last"])
        except (OSError, ValueError, KeyError, TypeError):
            last = 0
        n = max(last, _highest_existing(data_dir, platform, abbrev), floor) + 1
        while True:
            while (data_dir / platform / f"{abbrev}_{n}").exists() or (data_dir / "_failed" / platform / f"{abbrev}_{n}").exists():
                n += 1
            sd = data_dir / platform / f"{abbrev}_{n}"
            sd.parent.mkdir(parents=True, exist_ok=True)
            try:
                sd.mkdir()
                break
            except FileExistsError:
                n += 1
        tmp = cp.with_suffix(".tmp")
        tmp.write_text(json.dumps({"platform": platform, "abbrev": abbrev, "last": n}))
        os.replace(tmp, cp)
    return sd
