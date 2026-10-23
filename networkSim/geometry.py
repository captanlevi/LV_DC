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

Restart §7 (levi's prereg 2026-10-21; supervisor's ask): the trio no longer rotates. Each session's geometry is drawn
(draw_geometry), seeded per session, from a fixed distribution: equal weights (DRAW_WEIGHTS) over the platform's
GEOMETRY_CHOICES minus an excluded mode (the trio without --trio-yt-fullscreen: fullscreen), independent of the
steering target, of every count and of anything observed. yt caps quality by player size, so the draw decorrelates
resolution from rate exogenously; the yt Q_1080p floor fills at the draw's fullscreen rate (no coupling to the target).
The one-fullscreen token (one at a time in the trio): a fullscreen draw while a sibling holds it is redrawn by the same
generator among the support's other entries (weights renormalised), recorded as token_deferred. The record goes to
meta.json geometry_draw; the geometry the page took (CSS and device px) stays in meta.json geometry / player_pin.
Single mode keeps the rotation (next_geometry).
"""
from __future__ import annotations

import json
import random
from pathlib import Path

# t67 B1 (levi 19:03): a draw is NAMED by the player box it realises on chaos (DPR 1; the t58 record's median
# meta.geometry.player_client per geometry_target, non-_failed: yt n 6 / 4 / 7 / 6 / 3, tw 15 / 13, tt 11 / 12 / 5) and
# LAUNCHED by the instruction that produces it (the old name). twitch's two draws realised the same 1238x696, so tw keeps
# that one and adds player:854x480 at the same weight (levi: it also tests c15's "tw is not size-capped", measured at one
# box); its realised box is the A-window's to confirm (a different one is renamed by its own line).
INSTRUCTION: dict[str, dict[str, str]] = {
    "youtube": {"player:855x481": "player:853x480", "player:1263x710": "player:1280x720",
                "window:1263x710": "window:1920x1080", "theater:1243x699": "theater:1920x1080",
                "fullscreen:1920x1080": "fullscreen:1920x1080"},
    "twitch": {"player:1238x696": "player:1274x717", "player:854x480": "player:854x480"},   # chat open (CLOSE_CHAT_DEFAULT)
    "tiktok": {"player:783x604": "player:783x604", "player:537x728": "player:537x880", "window:1182x728": "window:1920x1080"},
}
GEOMETRY_CHOICES: dict[str, list[str]] = {p: list(m) for p, m in INSTRUCTION.items()}


# instructions no longer drawn, by the realised name of the box they gave (twitch's window draw realised 1238x696)
LEGACY_NAME: dict[str, dict[str, str]] = {"twitch": {"window:1920x1080": "player:1238x696"}}


def name_of(platform: str, spec: str | None) -> str | None:
    """A session's realised name from its launch instruction (the t58 record's geometry_target), else the spec as is."""
    if not spec:
        return spec
    rev = {v: k for k, v in INSTRUCTION.get(platform, {}).items()}
    return rev.get(spec) or LEGACY_NAME.get(platform, {}).get(spec) or spec


def instruction_of(platform: str, name: str | None) -> str | None:
    """The launch instruction of a realised name; any other spec (an instruction, a pin's raw spec) passes unchanged."""
    if not name:
        return name
    return INSTRUCTION.get(platform, {}).get(name, name)


# t67 B1 (levi 19:03): yt's tier cap by (mode, DPR), from the t58 record (n = 26 yt sessions): 1080 only in fullscreen
# (3 / 6 sessions vs 0 / 20 in every other draw; even the 855x481 box reached 720). DPR 1 only: a DPR 2 entry is set after
# its arm measures it (a missing key = no cap). twitch / tiktok: no cap (no evidence of one). Steering pulls no tier above
# the session's cap (steering.steer tier_cap).
YT_TIER_CAP: dict[tuple[str, float], str] = {("fullscreen", 1.0): "Q_1080p", ("player", 1.0): "Q_720p",
                                             ("window", 1.0): "Q_720p", ("theater", 1.0): "Q_720p"}


def tier_cap(platform: str, name: str | None, dpr: float = 1.0) -> str | None:
    if platform != "youtube" or not name:
        return None
    return YT_TIER_CAP.get((name.split(":", 1)[0], float(dpr)))


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


def _session_spec(meta: dict, platform: str | None = None) -> str | None:
    """The session's realised geometry name (t67 B1): v3 meta.geometry_name, else its launch instruction mapped back."""
    if meta.get("geometry_name"):
        return meta["geometry_name"]
    if meta.get("geometry_target"):
        return name_of(platform, meta["geometry_target"]) if platform else meta["geometry_target"]
    tgt = (meta.get("player_pin") or {}).get("target")
    spec = f"player:{tgt[0]}x{tgt[1]}" if tgt and len(tgt) == 2 else None
    return name_of(platform, spec) if platform else spec


def collected_counts(data_dir: Path, platform: str, select=None) -> dict[str, int]:
    """Accepted sessions per geometry entry (data/<...>/<platform>/<session>/meta.json; _failed not counted); select(meta)
    -> bool: only the sessions it keeps (the trio: its sessions of record, levi)."""
    counts = {g: 0 for g in GEOMETRY_CHOICES.get(platform, [])}
    for m in data_dir.rglob("meta.json"):
        if m.parent.parent.name != platform or "_failed" in m.parts or any(x.startswith("_trio") for x in m.relative_to(data_dir).parts):   # trio: its own root
            continue
        try:
            meta = json.loads(m.read_text())
            spec = _session_spec(meta, platform)
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


DRAW_RULE = ("restart §7: per session, seeded, over GEOMETRY_CHOICES minus excluded modes, independent of the steering "
             "target; equal weights except yt fullscreen at 1/3 of the full support (prereg d32a48ce, the 10-22 tip); a "
             "fullscreen draw while a sibling holds the token is redrawn among the others")
# prereg d32a48ce (label diversity at the 10-22 tip): yt's fullscreen draw weight is 1/3 of yt's full support, the four
# other entries sharing the rest equally; the token deferral still applies, so the realised share is reported, not assumed
YT_FULLSCREEN_SHARE = 1.0 / 3.0


def _weights(platform: str, choices: list[str]) -> dict[str, float]:
    if platform != "youtube":
        return {g: 1.0 for g in choices}
    rest = [g for g in choices if not g.startswith("fullscreen")]
    fs = [g for g in choices if g.startswith("fullscreen")]
    w = {g: 1.0 for g in rest}
    w.update({g: YT_FULLSCREEN_SHARE * len(rest) / (1.0 - YT_FULLSCREEN_SHARE) / len(fs) for g in fs})
    return w


DRAW_WEIGHTS: dict[str, dict[str, float]] = {p: _weights(p, gs) for p, gs in GEOMETRY_CHOICES.items()}


def draw_geometry(platform: str, seed: str, exclude_modes: tuple = (), token_held: bool = False) -> dict | None:
    """Restart §7: the session's geometry from its seed (orchestrate's per-session seed, salted with this draw's name);
    None for a platform with no entry left. token_held: a sibling holds the one-fullscreen token."""
    support = [g for g in GEOMETRY_CHOICES.get(platform) or [] if g.split(":")[0] not in exclude_modes]
    if not support:
        return None
    w = DRAW_WEIGHTS[platform]
    rng = random.Random(f"geometry:{seed}")
    drawn = rng.choices(support, weights=[w[g] for g in support], k=1)[0]
    rec = {"rule": DRAW_RULE, "seed": seed, "support": support,
           "weights": {g: round(w[g] / sum(w[x] for x in support), 6) for g in support},
           "excluded_modes": list(exclude_modes), "token_held": token_held, "drawn": drawn, "geometry": drawn}
    rest = [g for g in support if not g.startswith("fullscreen")]
    if token_held and drawn.startswith("fullscreen") and rest:
        rec.update(geometry=rng.choices(rest, weights=[w[g] for g in rest], k=1)[0], token_deferred=drawn)
    rec["instruction"] = instruction_of(platform, rec["geometry"])   # B1: the realised name drawn, launched by this
    return rec
