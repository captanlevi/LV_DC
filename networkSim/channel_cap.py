"""The twitch / tt channel cap (levi's ruling on peter's channel-diversity read, 2026-10-20, for the next count boundary;
channel identity only, never QoE, measured yield or labels). Their pools are open (chaos _trio, 2026-10-20: twitch `25`
sessions on `22` channels, max `3`; tt `19` on `16` hosts, max `2`), so no floor and no least-recently-drawn order (yt's
rule, yt_channels.next_channel): a cap alone. A channel (twitch) or room host (tt) with >= max(CAP_MIN, ceil(CAP_SHARE x
n_record)) sessions of record on its platform is passed over (excluded from the directory / room choice, as recent and
sibling streams are); recorded per session in meta.json channel_pick.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

CAP_SHARE = 0.10
CAP_MIN = 2
CAP_RULE = "levi 2026-10-20: twitch / tt open pools, a cap only: max(2, ceil(0.10 x n_record)) sessions of record per channel / host"


def channel_key(url: str | None) -> str | None:
    """twitch: the channel URL; tt: the room URL (its /@host/live); normalised as browser's exclusion lists are."""
    u = (url or "").split("?")[0].rstrip("/").lower()
    return u or None


def cap_state(trio_root: Path | None, platform: str, select=None) -> dict | None:
    """The platform's sessions of record under the trio root (accepted dirs, not _failed; select keeps the sessions of
    record), per channel key: n_record, the cap, and the keys at or above it. None outside the trio."""
    if trio_root is None or platform not in ("twitch", "tiktok"):
        return None
    counts: dict[str, int] = {}
    for m in sorted((Path(trio_root) / platform).glob("*/meta.json")):
        try:
            meta = json.loads(m.read_text())
        except (OSError, ValueError):
            continue
        if select is not None and not select(meta):
            continue
        k = channel_key(meta.get("channel_url"))
        if k:
            counts[k] = counts.get(k, 0) + 1
    n = sum(counts.values())
    cap = max(CAP_MIN, math.ceil(CAP_SHARE * n))
    return {"rule": CAP_RULE, "n_record": n, "cap": cap, "capped": sorted(k for k, v in counts.items() if v >= cap),
            "max_count": max(counts.values(), default=0)}
