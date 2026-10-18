"""tw_categories.py: the twitch category rotation (LVQoE prereg addendum O, 2026-10-17; rushi via supervisor).

Content diversity for twitch, like yt's channel types: each session draws a category from a fixed list, each with a
content type (talk, fast gaming, slow / strategy gaming, esports, music / creative, IRL). The type is assigned here,
before capture, and recorded in meta.json (tw_category, tw_category_type); it is never inferred from the data.

esports (levi 2026-10-17): not a directory (its top card need not be a broadcast, and the type would then be wrong) but
a fixed list of official league channels, drawn only when one of them is live (checked from the channel page's
isLiveBroadcast before the session, outside the capture); when none is, esports is skipped for that session, the next
type is drawn, and the skip is logged (data/_summaries/tw_esports_skips.jsonl, shown in the daily summary).

Rotation: the least-collected category next, list order breaking ties. Sessions are counted from every
twitch/<session>/meta.json under data/ (tw_category), excluding data/_failed. Twitch sessions collected before the
rotation went to the Just Chatting directory (browser.LIVE_URLS), so they count as Just Chatting and the rotation
first evens the other categories out. Within a category the session takes the directory's first live card (the most
viewers), as before.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

TW_CATEGORIES: list[dict] = [
    {"name": "Just Chatting", "type": "talk"},
    {"name": "Counter-Strike", "type": "gaming_fast"},
    {"name": "Chess", "type": "gaming_slow"},
    {"name": "esports", "type": "esports", "channels": ["riotgames", "lck", "lec", "eslcs", "valorant"]},
    {"name": "Music", "type": "music_creative"},
    {"name": "IRL", "type": "irl"},
]
PRE_ROTATION_CATEGORY = "Just Chatting"


def url_of(name: str) -> str:
    return f"https://www.twitch.tv/directory/game/{quote(name)}"


def channel_url(channel: str) -> str:
    return f"https://www.twitch.tv/{channel}"


def twitch_live(channel: str, timeout: float = 10.0) -> bool:
    """True when the channel page's JSON-LD says the channel is broadcasting (isLiveBroadcast); False on any error."""
    try:
        req = urllib.request.Request(channel_url(channel), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read(2_000_000).decode("utf-8", "replace")
        return re.search(r'"isLiveBroadcast"\s*:\s*true', html) is not None
    except Exception:
        return False


def by_name(name: str) -> dict | None:
    return next((c for c in TW_CATEGORIES if c["name"] == name), None)


def collected_counts(data_dir: Path) -> dict[str, int]:
    counts = {c["name"]: 0 for c in TW_CATEGORIES}
    for m in data_dir.rglob("meta.json"):
        if m.parent.parent.name != "twitch" or "_failed" in m.parts or "_trio" in m.parts:   # trio: its own root
            continue
        try:
            name = json.loads(m.read_text()).get("tw_category") or PRE_ROTATION_CATEGORY
        except Exception:
            continue
        if name in counts:
            counts[name] += 1
    return counts


def last_category(data_dir: Path, skip: Path | None = None) -> str | None:
    """The category of the latest twitch session (data/twitch and data/_failed/twitch, by meta.json started_at)."""
    best = (None, -1.0)
    for m in list((data_dir / "twitch").glob("*/meta.json")) + list((data_dir / "_failed" / "twitch").glob("*/meta.json")):
        if skip is not None and m.parent.resolve() == Path(skip).resolve():
            continue
        try:
            meta = json.loads(m.read_text())
        except Exception:
            continue
        t = float(meta.get("started_at") or 0)
        if t > best[1]:
            best = (meta.get("tw_category") or PRE_ROTATION_CATEGORY, t)
    return best[0]


def next_category(data_dir: Path, exclude: set[str] | None = None, live_check=twitch_live) -> tuple[dict, dict | None]:
    """(category, esports skip record or None): the least-collected category not in `exclude` (a category or channel
    with no playable stream just now). esports is taken only with a live league channel (category["channel"]);
    otherwise it is skipped and the next one drawn."""
    exclude = set(exclude or ())
    counts = collected_counts(data_dir)
    # never the previous session's category (supervisor 2026-10-17: a failed tw_10 left Chess least-collected, and tw_11
    # drew it again), unless it is the only one left
    prev = last_category(data_dir)
    pool = ([c for c in TW_CATEGORIES if c["name"] not in exclude | {prev}]
            or [c for c in TW_CATEGORIES if c["name"] not in exclude] or TW_CATEGORIES)
    skipped = None
    for c in sorted(pool, key=lambda c: (counts[c["name"]], TW_CATEGORIES.index(c))):
        if c["type"] != "esports":
            return c, skipped
        live = next((ch for ch in c["channels"] if ch not in exclude and live_check(ch)), None)
        if live:
            return {**c, "channel": live}, skipped
        skipped = {"ts": time.time(), "reason": "esports_unavailable", "checked": c["channels"]}
    return TW_CATEGORIES[0], skipped
