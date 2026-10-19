"""geometry.py: per-session window / player geometry for collection v2 (rushi via supervisor, t37, 2026-10-16).

Geometry is not pinned: diversity is wanted. Each session draws one entry from its platform's fixed list, equal shares,
rotated like the yt channels (the least-collected entry next, list order breaking ties). The draw is recorded in
meta.json (geometry_target); client_w / client_h per tick are in labels.csv already.

  player:WxH  the window is resized until the <video> element measures W x H (browser.pin_player)
  window:WxH  the window is set to W x H (clamped to the screen), the player takes what the page layout gives it
  theater:WxH (yt only) the window as window:WxH, then yt's theater mode (the player's size button), so the player
              exceeds 720 device px tall where the viewport allows (supervisor 2026-10-18: no 1080p in 30 sessions,
              the 1263x710 / 855x481 players most likely capped yt's ABR at 720p)
  fullscreen:WxH (yt only) the window as window:WxH, then the player's own fullscreen (the `f` key, a CDP key event:
              a user gesture), the pointer parked over the picture so the controls auto-hide: the player is the screen
              (1080 device px tall on chaos's 1920x1080 panel) and the X11 shot is the whole screen (supervisor t37,
              2026-10-18: it replaces theater:1920x1080@1.5 in the rotation)
  <spec>@D    the same, with Chrome launched at device scale factor D (--force-device-scale-factor; layout only):
              theater:1920x1080@1.5 (supervisor 2026-10-18, shagun's audit: in theater mode yt_39-41's player stayed
              699 device px tall in the 1818x868 viewport and never played above 720p, so the yt 1080p floor could not
              fill); the DPR and the player's device px are recorded in meta.json player_pin / geometry (Q am. 2).
              Retired after yt_43 (supervisor t37): at scale 1.5 the CSS viewport shrinks to 1235x702 on a 1920x1080
              panel, so the theater player is 853x480 CSS = 1280x720 device px, the same 720 cap; its sessions stay
              valid at their recorded geometry

Sessions collected before rotation (pinned sizes) carry only player_pin.target in meta.json; they count as
player:<target>, so the rotation first evens the other entries out.
"""
from __future__ import annotations

import json
from pathlib import Path

GEOMETRY_CHOICES: dict[str, list[str]] = {
    "youtube": ["player:853x480", "player:1280x720", "window:1920x1080", "theater:1920x1080", "fullscreen:1920x1080"],
    "twitch": ["player:1274x717", "window:1920x1080"],          # chat open on twitch (CLOSE_CHAT_DEFAULT)
    "tiktok": ["player:783x604", "player:537x880", "window:1920x1080"],
}


def parse(spec: str) -> tuple[str, int, int]:
    mode, wh = spec.split("@", 1)[0].split(":", 1)
    w, h = wh.lower().split("x")
    if mode not in ("player", "window", "theater", "fullscreen"):
        raise ValueError(f"geometry mode must be player, window, theater or fullscreen: {spec}")
    return mode, int(w), int(h)


def device_scale(spec: str | None) -> float | None:
    """The `@D` device scale factor of a spec (theater:1920x1080@1.5 -> 1.5); None without one."""
    if not spec or "@" not in spec:
        return None
    d = float(spec.split("@", 1)[1])
    if not 1.0 <= d <= 3.0:
        raise ValueError(f"device scale factor out of range: {spec}")
    return d


# hot-fix 4 (t37, shagun on yt_46): the collection Chrome runs with --no-sandbox, so Chrome shows its "unsupported
# command-line flag" infobar (56 px) at the top of the window, also in window fullscreen: the viewport never covers the
# screen (1920x1024 on the 1920x1080 panel) and the player is never the screen. --test-type hides that infobar (and the
# other startup infobars; ChromeDriver passes it to every Chrome it drives). Fullscreen sessions only: every other
# geometry's launch, and so its viewport, is unchanged (the infobar has been there in all of them).
MODE_CHROME_ARGS: dict[str, list[str]] = {"fullscreen": ["--test-type"]}


def chrome_args(spec: str | None) -> list[str]:
    """The Chrome launch arguments a spec needs (the device scale factor; fullscreen's --test-type; layout only)."""
    d = device_scale(spec)
    out = [f"--force-device-scale-factor={d:g}"] if d else []
    if spec:
        out += MODE_CHROME_ARGS.get(parse(spec)[0], [])
    return out


def start_chrome_make_vars(session_dir, chrome_profile, window_size: str | None, platform: str | None,
                           geometry_spec: str | None, extra_args: list[str] | None = None) -> list[str]:
    """exp.py's `make start_chrome` variables (hot-fix 4: one definition, so ops/diag/fullscreen_check.py launches its
    Chrome with exactly the collector's arguments). extra_args: the trio "fb" layout's window position (single mode:
    none)."""
    import decode_path
    return ([f"SESSION_DIR={session_dir}", f"CHROME_PROFILE={chrome_profile}"]
            + ([f"WINDOW_SIZE={window_size}"] if window_size else [])
            + merge_make_vars(decode_path.make_vars(platform or ""), chrome_args(geometry_spec) + list(extra_args or [])))


def merge_make_vars(make_vars: list[str], extra_args: list[str]) -> list[str]:
    """exp.py's `make start_chrome` variables (decode_path.make_vars) with extra Chrome args appended to
    CHROME_EXTRA_ARGS (one variable: make takes the last assignment)."""
    if not extra_args:
        return list(make_vars)
    out, done = [], False
    for v in make_vars:
        if v.startswith("CHROME_EXTRA_ARGS="):
            v, done = v + " " + " ".join(extra_args), True
        out.append(v)
    return out if done else out + ["CHROME_EXTRA_ARGS=" + " ".join(extra_args)]


def _session_spec(meta: dict) -> str | None:
    if meta.get("geometry_target"):
        return meta["geometry_target"]
    tgt = (meta.get("player_pin") or {}).get("target")
    return f"player:{tgt[0]}x{tgt[1]}" if tgt and len(tgt) == 2 else None


def collected_counts(data_dir: Path, platform: str, select=None) -> dict[str, int]:
    """Accepted sessions per geometry entry (data/<...>/<platform>/<session>/meta.json; _failed not counted); select(meta)
    -> bool: only the sessions it keeps (the trio: its sessions of record, levi)."""
    counts = {g: 0 for g in GEOMETRY_CHOICES.get(platform, [])}
    for m in data_dir.rglob("meta.json"):
        if m.parent.parent.name != platform or "_failed" in m.parts or any(x.startswith("_trio") for x in m.relative_to(data_dir).parts):   # trio: its own root
            continue
        try:
            meta = json.loads(m.read_text())
            spec = _session_spec(meta)
        except Exception:
            continue
        if select is not None and not select(meta):
            continue
        if spec in counts:
            counts[spec] += 1
    return counts


def next_geometry(data_dir: Path, platform: str, exclude_modes: tuple = (), select=None) -> str | None:
    """exclude_modes (t53 trio v1: ("fullscreen",)): rotation entries of those modes are not drawn."""
    choices = [g for g in GEOMETRY_CHOICES.get(platform) or [] if g.split(":")[0] not in exclude_modes]
    if not choices:
        return None
    counts = collected_counts(data_dir, platform, select)
    return min(choices, key=lambda g: (counts[g], choices.index(g)))
