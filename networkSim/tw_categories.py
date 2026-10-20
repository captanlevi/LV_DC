"""tw_categories.py: the twitch category rotation (LVQoE prereg addendum O, 2026-10-17; rushi via supervisor).

Content diversity for twitch, like yt's channel types: each session draws a category from a fixed list, each with a
content type (talk, fast gaming, slow / strategy gaming, esports, music / creative, IRL). The type is assigned here,
before capture, and recorded in meta.json (tw_category, tw_category_type); it is never inferred from the data.

esports (levi 2026-10-17): not a directory (its top card need not be a broadcast, and the type would then be wrong) but
a fixed list of official league channels, drawn only when one of them is live (checked from the channel page's
isLiveBroadcast before the session, outside the capture); when none is, esports is skipped for that session, the next
type is drawn, and the skip is logged (data/_summaries/tw_esports_skips.jsonl, shown in the daily summary).

Rotation: the least-collected category next, ties to a seeded draw (restart prereg §5; list order before). Sessions are
counted from every twitch/<session>/meta.json under data/ (tw_category), excluding data/_failed. Twitch sessions
collected before the rotation went to the Just Chatting directory (browser.LIVE_URLS), so they count as Just Chatting
and the rotation first evens the other categories out. Within a category the session takes the directory's first live
card (the most viewers) not at the channel cap, as before.

The category floor (restart prereg §5, CATEGORY_RULE): every listed category reaches CATEGORY_FLOOR sessions before
any gets a 4th (a category at >= CATEGORY_HOLD_AT is passed over while one is below the floor), and the floor outranks
the no-immediate-repeat rule. A category with no playable stream at the pick (excluded, or esports with no live league
channel below the cap) cannot block the rotation: the pick then takes a held category and records `floor_open`. Every
stage goes to LAST_PICK -> orchestrate's pick record -> meta.json channel_pick.category.
"""
from __future__ import annotations

import json
import random
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
CATEGORY_FLOOR = 2
CATEGORY_HOLD_AT = 3
CATEGORY_TIE_SEED = 0
CATEGORY_RULE = ("restart prereg §5: least-collected, seeded tie (CATEGORY_TIE_SEED + the root's twitch session dirs); "
                 "every category reaches 2 sessions of record before any gets a 4th, the floor before the no-repeat "
                 "rule; an unavailable category cannot block (floor_open); the channel cap within")
TIERS = ("rotation", "floor_over_repeat", "floor_open", "floor_open_repeat", "all_excluded")
LAST_PICK: dict = {}


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


def collected_counts(data_dir: Path, select=None) -> dict[str, int]:
    """select(meta) -> bool: only the sessions it keeps (the trio: its sessions of record, levi)."""
    counts = {c["name"]: 0 for c in TW_CATEGORIES}
    for m in data_dir.rglob("meta.json"):
        if m.parent.parent.name != "twitch" or "_failed" in m.parts or any(x.startswith("_trio") for x in m.relative_to(data_dir).parts):   # trio: its own root
            continue
        try:
            meta = json.loads(m.read_text())
        except Exception:
            continue
        if select is not None and not select(meta):
            continue
        if not meta.get("tw_category") and not (m.parent / "check.json").exists():
            continue                     # t58 L-7: a running session whose category is not written yet, not Just Chatting
        name = meta.get("tw_category") or PRE_ROTATION_CATEGORY
        if name in counts:
            counts[name] += 1
    return counts


def last_category(data_dir: Path, skip: Path | None = None, select=None) -> str | None:
    """The category of the latest twitch session (data/twitch and data/_failed/twitch, by meta.json started_at)."""
    best = (None, -1.0)
    for m in list((data_dir / "twitch").glob("*/meta.json")) + list((data_dir / "_failed" / "twitch").glob("*/meta.json")):
        if skip is not None and m.parent.resolve() == Path(skip).resolve():
            continue
        try:
            meta = json.loads(m.read_text())
        except Exception:
            continue
        if select is not None and not select(meta):
            continue
        t = float(meta.get("started_at") or 0)
        if t > best[1]:
            best = (meta.get("tw_category") or PRE_ROTATION_CATEGORY, t)
    return best[0]


def draw_seed(data_dir: Path) -> int:
    """CATEGORY_TIE_SEED + every twitch session dir under data_dir (accepted and _failed): one seed per draw, as
    yt_channels.draw_seed."""
    root = Path(data_dir)
    return CATEGORY_TIE_SEED + sum(1 for d in list((root / "twitch").glob("tw_*")) + list((root / "_failed" / "twitch").glob("tw_*"))
                                   if d.is_dir())


def next_category(data_dir: Path, exclude: set[str] | None = None, live_check=twitch_live,
                  select=None, capped: set[str] | frozenset = frozenset()) -> tuple[dict, dict | None]:
    """(category, esports skip record or None) by CATEGORY_RULE (the module docstring). `exclude`: categories or
    channels with no playable stream just now; `capped`: channel keys at the twitch channel cap (channel_cap), never
    an esports league channel. esports is taken only with a live league channel (category["channel"]); otherwise it is
    skipped and the next one drawn. The pick's state goes to LAST_PICK."""
    import channel_cap
    exclude = set(exclude or ())
    counts = collected_counts(data_dir, select)
    # never the previous session's category (supervisor 2026-10-17: a failed tw_10 left Chess least-collected, and tw_11
    # drew it again), unless it is the only one left or the floor needs it (a repeat before a 4th)
    prev = last_category(data_dir, select=select)
    seed = draw_seed(data_dir)
    rnd = random.Random(seed)
    key = {c["name"]: rnd.random() for c in TW_CATEGORIES}          # the seeded tie (list order fixes the draws)
    below = sorted(n for n, v in counts.items() if v < CATEGORY_FLOOR)
    held = {n for n, v in counts.items() if below and v >= CATEGORY_HOLD_AT}
    avail = [c for c in TW_CATEGORIES if c["name"] not in exclude]
    tiers = ([c for c in avail if c["name"] != prev and c["name"] not in held],
             [c for c in avail if c["name"] == prev and c["name"] not in held],
             [c for c in avail if c["name"] != prev and c["name"] in held],
             [c for c in avail if c["name"] == prev and c["name"] in held],
             [c for c in TW_CATEGORIES if c["name"] in exclude])
    skipped, drawn, tier = None, None, None
    for t, cands in enumerate(tiers):
        for c in sorted(cands, key=lambda c: (counts[c["name"]], key[c["name"]])):
            if c["type"] != "esports":
                drawn, tier = c, t
                break
            free = [ch for ch in c["channels"] if channel_cap.channel_key(channel_url(ch)) not in set(capped)]
            live = next((ch for ch in free if ch not in exclude and live_check(ch)), None)
            if live:
                drawn, tier = {**c, "channel": live}, t
                break
            skipped = {"ts": time.time(), "reason": "esports_unavailable", "checked": free,
                       **({"capped": sorted(set(c["channels"]) - set(free))} if len(free) < len(c["channels"]) else {})}
        if drawn is not None:
            break
    if drawn is None:                     # nothing playable at all: as before, the list's first
        drawn = TW_CATEGORIES[0]
    same = [c["name"] for c in tiers[tier] if counts[c["name"]] == counts[drawn["name"]]] if tier is not None else []
    LAST_PICK.clear()
    LAST_PICK.update(rule=CATEGORY_RULE, seed=seed, floor=CATEGORY_FLOOR, hold_at=CATEGORY_HOLD_AT, counts=counts,
                     below_floor=below, held=sorted(held), previous=prev, excluded=sorted(exclude & set(counts)),
                     stage=TIERS[tier] if tier is not None else "fallback", tied=sorted(same), drawn=drawn["name"], channel=drawn.get("channel"),
                     **({"esports_skip": skipped} if skipped else {}), **({"cap_excluded": sorted(capped)} if capped else {}))
    return drawn, skipped
