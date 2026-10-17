"""yt_channels.py: the collection v2 YouTube channel list and its rotation (LVQoE prereg collection_v2 §1 / G5, levi R2).

Criteria: 24/7 live, public, not age-gated or member-only. Content types: news, sport, gaming, music, nature_cams, each
with 2 primary channels from different owners. The content type is assigned here, before capture, and recorded in
meta.json; it is never inferred from the data. Spares substitute for a primary of the same type that is not live.

Verified 2026-10-16 on the laptop (Chrome 155; scripts in the LVQoE t35 report): every entry's /live page showed the
live badge, playback advanced, and no age or members gate appeared.

Rotation: no channel gets a 3rd session until every primary has 2 (generally: always the least-collected primary,
ties broken by list order). Sessions are counted from every youtube/<session>/meta.json under data/ (channel_handle),
excluding data/_failed (a failed session does not count).

Not-live skips (shagun 2026-10-17: @Sypherpkvods, never collected, so always least-collected, failed not-live on yt_7,
yt_12 and yt_30): read from the not-live failures archived in data/_failed, so they survive a batch restart. One in the
last NOT_LIVE_RETRY_S skips the channel that long; NOT_LIVE_STRIKES within NOT_LIVE_OUT_S put it out for NOT_LIVE_OUT_S
after the latest.
"""
from __future__ import annotations

import json
from pathlib import Path

NOT_LIVE_MARK = "Failed to navigate to a live"   # browser.py's start failure, kept in meta.json abort_reason
NOT_LIVE_RETRY_S = 6 * 3600
NOT_LIVE_STRIKES = 2
NOT_LIVE_OUT_S = 24 * 3600

YT_CHANNELS: list[dict] = [
    {"handle": "@AlJazeeraEnglish", "type": "news", "owner": "Al Jazeera Media Network", "role": "primary"},
    {"handle": "@dwnews", "type": "news", "owner": "Deutsche Welle", "role": "primary"},
    {"handle": "@MonsterJam", "type": "sport", "owner": "Feld Entertainment (Monster Jam)", "role": "primary"},
    {"handle": "@FUELTVMoto", "type": "sport", "owner": "FUEL TV", "role": "primary"},
    {"handle": "@Sypherpkvods", "type": "gaming", "owner": "SypherPK", "role": "primary"},
    {"handle": "@RetroWorldVideos", "type": "gaming", "owner": "RetroWorld", "role": "primary"},
    {"handle": "@LofiGirl", "type": "music", "owner": "Lofi Girl", "role": "primary"},
    {"handle": "@everybodychillradio", "type": "music", "owner": "Everybody Chill Radio", "role": "primary"},
    {"handle": "@SBC-TV1", "type": "nature_cams", "owner": "SBC-TV (Maasai Mara Live Safari)", "role": "primary"},
    {"handle": "@ScottishWildlifeGarden", "type": "nature_cams", "owner": "Scottish Wildlife Garden", "role": "primary"},
    {"handle": "@channelnewsasia", "type": "news", "owner": "Mediacorp (CNA)", "role": "spare"},
    {"handle": "@FuboSports", "type": "sport", "owner": "Fubo Sports Network", "role": "spare"},
    {"handle": "@BeanBoltTV", "type": "gaming", "owner": "BeanBoltTV", "role": "spare"},
    {"handle": "@SkyofFairyTales", "type": "music", "owner": "Sky of Fairy Tales", "role": "spare"},
    {"handle": "@dakotaspond", "type": "nature_cams", "owner": "Dakota's Pond", "role": "spare"},
    {"handle": "@TipTank247", "type": "nature_cams", "owner": "TipTank247", "role": "spare"},
]


def url_of(handle: str) -> str:
    return f"https://www.youtube.com/{handle}/live"


def by_handle(handle: str) -> dict | None:
    return next((c for c in YT_CHANNELS if c["handle"] == handle), None)


def collected_counts(data_dir: Path) -> dict[str, int]:
    """Accepted yt sessions per channel handle: every <...>/youtube/<session>/meta.json under data_dir (so both
    data/youtube/ and data/<collector>/youtube/ count), except anything under _failed/."""
    counts = {c["handle"]: 0 for c in YT_CHANNELS}
    metas = (m for m in data_dir.rglob("meta.json") if m.parent.parent.name == "youtube" and "_failed" not in m.parts)
    for meta in sorted(metas):
        try:
            m = json.loads(meta.read_text())
        except Exception:
            continue
        h = m.get("channel_handle")
        if h in counts:
            counts[h] += 1
    return counts


def not_live_failures(data_dir: Path) -> dict[str, list[float]]:
    """Start times of the not-live failures per channel handle: data/_failed/.../youtube/<session>/meta.json."""
    out: dict[str, list[float]] = {}
    metas = (m for m in data_dir.rglob("meta.json") if m.parent.parent.name == "youtube" and "_failed" in m.parts)
    for meta in metas:
        try:
            m = json.loads(meta.read_text())
        except Exception:
            continue
        if NOT_LIVE_MARK in (m.get("abort_reason") or "") and m.get("channel_handle") and m.get("started_at"):
            out.setdefault(m["channel_handle"], []).append(float(m["started_at"]))
    return out


def not_live_skips(data_dir: Path, now: float) -> dict[str, str]:
    """Channels skipped now for recent not-live failures, with why (module docstring)."""
    skips = {}
    for h, ts in not_live_failures(data_dir).items():
        last = max(ts)
        n = sum(now - t < NOT_LIVE_OUT_S for t in ts)
        if n >= NOT_LIVE_STRIKES and now - last < NOT_LIVE_OUT_S:
            skips[h] = f"{n} not-live failures in {NOT_LIVE_OUT_S // 3600} h: out until {NOT_LIVE_OUT_S // 3600} h after the last"
        elif now - last < NOT_LIVE_RETRY_S:
            skips[h] = f"not live {(now - last) / 3600:.1f} h ago: retry after {NOT_LIVE_RETRY_S // 3600} h"
    return skips


def next_channel(data_dir: Path, exclude: set[str] | None = None) -> dict:
    """The least-collected primary (list order breaks ties); a spare of the same type only when every primary of
    that type is excluded (e.g. not live right now)."""
    exclude = exclude or set()
    counts = collected_counts(data_dir)
    prim = [c for c in YT_CHANNELS if c["role"] == "primary"]
    order = sorted(range(len(prim)), key=lambda i: (counts[prim[i]["handle"]], i))
    for i in order:
        c = prim[i]
        if c["handle"] not in exclude:
            return c
        if any(p["type"] == c["type"] and p["handle"] not in exclude for p in prim):
            continue
        spares = [s for s in YT_CHANNELS if s["role"] == "spare" and s["type"] == c["type"] and s["handle"] not in exclude]
        if spares:
            return sorted(spares, key=lambda s: counts[s["handle"]])[0]
    raise RuntimeError("no live channel left in the list")
