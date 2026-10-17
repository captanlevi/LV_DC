"""tw_categories.py: the twitch category rotation (LVQoE prereg addendum O, 2026-10-17; rushi via supervisor).

Content diversity for twitch, like yt's channel types: each session draws a category from a fixed list, each with a
content type (talk, fast gaming, slow / strategy gaming, esports, music / creative, IRL). The type is assigned here,
before capture, and recorded in meta.json (tw_category, tw_category_type); it is never inferred from the data.

Rotation: the least-collected category next, list order breaking ties. Sessions are counted from every
twitch/<session>/meta.json under data/ (tw_category), excluding data/_failed. Twitch sessions collected before the
rotation went to the Just Chatting directory (browser.LIVE_URLS), so they count as Just Chatting and the rotation
first evens the other categories out. Within a category the session takes the directory's first live card (the most
viewers), as before.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

TW_CATEGORIES: list[dict] = [
    {"name": "Just Chatting", "type": "talk"},
    {"name": "Counter-Strike", "type": "gaming_fast"},
    {"name": "Chess", "type": "gaming_slow"},
    # esports: the category whose top live channels are most often tournament / league broadcasts; provisional until
    # levi confirms the list (a pro broadcast is not guaranteed at every hour)
    {"name": "League of Legends", "type": "esports"},
    {"name": "Music", "type": "music_creative"},
    {"name": "IRL", "type": "irl"},
]
PRE_ROTATION_CATEGORY = "Just Chatting"


def url_of(name: str) -> str:
    return f"https://www.twitch.tv/directory/game/{quote(name)}"


def by_name(name: str) -> dict | None:
    return next((c for c in TW_CATEGORIES if c["name"] == name), None)


def collected_counts(data_dir: Path) -> dict[str, int]:
    counts = {c["name"]: 0 for c in TW_CATEGORIES}
    for m in data_dir.rglob("meta.json"):
        if m.parent.parent.name != "twitch" or "_failed" in m.parts:
            continue
        try:
            name = json.loads(m.read_text()).get("tw_category") or PRE_ROTATION_CATEGORY
        except Exception:
            continue
        if name in counts:
            counts[name] += 1
    return counts


def next_category(data_dir: Path, exclude: set[str] | None = None) -> dict:
    """The least-collected category not in `exclude` (e.g. one whose directory had no playable stream just now)."""
    exclude = exclude or set()
    counts = collected_counts(data_dir)
    pool = [c for c in TW_CATEGORIES if c["name"] not in exclude] or TW_CATEGORIES
    return min(pool, key=lambda c: (counts[c["name"]], TW_CATEGORIES.index(c)))
