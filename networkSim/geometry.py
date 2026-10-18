"""geometry.py: per-session window / player geometry for collection v2 (rushi via supervisor, t37, 2026-10-16).

Geometry is not pinned: diversity is wanted. Each session draws one entry from its platform's fixed list, equal shares,
rotated like the yt channels (the least-collected entry next, list order breaking ties). The draw is recorded in
meta.json (geometry_target); client_w / client_h per tick are in labels.csv already.

  player:WxH  the window is resized until the <video> element measures W x H (browser.pin_player)
  window:WxH  the window is set to W x H (clamped to the screen), the player takes what the page layout gives it
  theater:WxH (yt only) the window as window:WxH, then yt's theater mode (the player's size button), so the player
              exceeds 720 device px tall where the viewport allows (supervisor 2026-10-18: no 1080p in 30 sessions,
              the 1263x710 / 855x481 players most likely capped yt's ABR at 720p)

Sessions collected before rotation (pinned sizes) carry only player_pin.target in meta.json; they count as
player:<target>, so the rotation first evens the other entries out.
"""
from __future__ import annotations

import json
from pathlib import Path

GEOMETRY_CHOICES: dict[str, list[str]] = {
    "youtube": ["player:853x480", "player:1280x720", "window:1920x1080", "theater:1920x1080"],
    "twitch": ["player:1274x717", "window:1920x1080"],          # chat open on twitch (CLOSE_CHAT_DEFAULT)
    "tiktok": ["player:783x604", "player:537x880", "window:1920x1080"],
}


def parse(spec: str) -> tuple[str, int, int]:
    mode, wh = spec.split(":", 1)
    w, h = wh.lower().split("x")
    if mode not in ("player", "window", "theater"):
        raise ValueError(f"geometry mode must be player, window or theater: {spec}")
    return mode, int(w), int(h)


def _session_spec(meta: dict) -> str | None:
    if meta.get("geometry_target"):
        return meta["geometry_target"]
    tgt = (meta.get("player_pin") or {}).get("target")
    return f"player:{tgt[0]}x{tgt[1]}" if tgt and len(tgt) == 2 else None


def collected_counts(data_dir: Path, platform: str) -> dict[str, int]:
    """Accepted sessions per geometry entry (data/<...>/<platform>/<session>/meta.json; _failed not counted)."""
    counts = {g: 0 for g in GEOMETRY_CHOICES.get(platform, [])}
    for m in data_dir.rglob("meta.json"):
        if m.parent.parent.name != platform or "_failed" in m.parts:
            continue
        try:
            spec = _session_spec(json.loads(m.read_text()))
        except Exception:
            continue
        if spec in counts:
            counts[spec] += 1
    return counts


def next_geometry(data_dir: Path, platform: str) -> str | None:
    choices = GEOMETRY_CHOICES.get(platform)
    if not choices:
        return None
    counts = collected_counts(data_dir, platform)
    return min(choices, key=lambda g: (counts[g], choices.index(g)))
