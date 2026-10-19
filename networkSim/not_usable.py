"""not_usable.py: rotation hygiene for the trio (levi / supervisor 2026-10-19, after counted round 1's yt_100007 on
@FuboSports: usable 0.22, startup_failure): a yt channel, twitch channel or tt room with NOT_USABLE_FAILURES
startup_failure sessions of record in the trio root (accepted or _failed; record.is_record, t58 L-3 / L-5: one membership
rule with the targets and the steering) is set aside, never picked, until a manual check clears
it. A manual check is a line in the trio root's _summaries/not_usable_cleared.json, {key: {"cleared_at": <epoch s>,
"by": ..., "note": ...}}; only startup_failures after cleared_at count again. Each pick records the keys set aside, with
their sessions. Single mode is unchanged (its own root is never read here).
"""
from __future__ import annotations

import json
from pathlib import Path

import record

NOT_USABLE_FAILURES = 2
CLEARED_FILE = Path("_summaries") / "not_usable_cleared.json"


def _norm(u: str | None) -> str:
    return (u or "").split("?")[0].rstrip("/").lower()


def key_of(platform: str, meta: dict) -> str | None:
    """The rotation key a session ran on: yt's channel handle, a twitch channel's or tt room's URL (normalised)."""
    if platform == "youtube":
        return meta.get("channel_handle") or None
    return _norm(meta.get("channel_url")) or None


def scan(trio_root: Path, platform: str) -> dict[str, list[str]]:
    """{key: [sessions]} of the trio root's startup_failure sessions (check.json session_class) on `platform`, after
    each key's manual clear."""
    root = Path(trio_root)
    try:
        cleared = json.loads((root / CLEARED_FILE).read_text())
    except (OSError, ValueError):
        cleared = {}
    out: dict[str, list[tuple[float, str]]] = {}
    for d in list((root / platform).glob("*")) + list((root / "_failed" / platform).glob("*")):
        try:
            chk = json.loads((d / "check.json").read_text())
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        if chk.get("session_class") != "startup_failure" or not record.is_record(meta):
            continue                           # t58 L-3 / L-5: sessions of record only (one membership rule)
        k = key_of(platform, meta)
        t = float(meta.get("started_at") or 0)
        if k and t > float((cleared.get(k) or {}).get("cleared_at") or 0):
            out.setdefault(k, []).append((t, d.name))
    return {k: [n for _, n in sorted(v)] for k, v in out.items()}


def set_aside(trio_root: Path, platform: str) -> dict[str, list[str]]:
    """The keys not to pick: those with >= NOT_USABLE_FAILURES startup_failures since their manual clear."""
    return {k: v for k, v in scan(trio_root, platform).items() if len(v) >= NOT_USABLE_FAILURES}


def trio_root_of(session_dir: Path | None) -> Path | None:
    """The session's root when it is the trio's (data/_trio[...]/<platform>/<session>), else None (single mode)."""
    if session_dir is None:
        return None
    root = Path(session_dir).resolve().parent.parent
    return root if root.name.startswith("_trio") else None
