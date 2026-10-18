"""stuck.py: the stuck-player rule (LVQoE collection prereg Addendum Q amendments 5-6, 2026-10-18; rushi's question on a
5-minute twitch stall; shagun's tw_1 / tw_27 reads: after a down-switch under starvation the twitch player held ~24 s of
media and never fed the element again, frozen for 10+ min with the shaper back at 2-4x the lowest rung).

- The frozen measure is content.js's lvqoeFrozenS (= check_session.frozen_runs): the backstop watchdog and this rule
  read the same run, so they cannot disagree.
- T2 clock = now - max(the frozen run's start, the start of the current stretch of shaper labels above the platform's
  lowest-rung bitrate). Below the rung (or the rung unknown) there is no T2: a starved player is not a stuck one.
- First T2 >= T2_S: one re-entry of the same channel / room (recorded as an entry with recovered_frozen_s and its
  window); playback must resume within REENTRY_CONFIRM_S. A second T2, or no resume: player_frozen, censored (time and
  shaper state); the session ends. At most RECOVERIES_PER_SESSION recovery of either kind (tt's player_dead re-entry
  included). Labels are unchanged; the part of the frozen window after the shaper went back above the rung is recorded.

The lowest rung: tt from the room data's sdk_params vbitrate (the played codec's ladder; video only, tt's audio
bitrate is not in the room data), twitch from the HLS master playlist's lowest BANDWIDTH (audio included), yt from the
lowest offered video format's bitrate plus the lowest audio format's; FALLBACK_RUNG_KBIT when none was read (source
recorded either way). The shaper's rate is compared as is (no protocol-overhead margin).
"""
from __future__ import annotations

import json
import re

T2_S = 90.0
REENTRY_CONFIRM_S = 60.0
RECOVERIES_PER_SESSION = 1
# a fallback only (source "fallback"): twitch's transcode ladder's 160p30 (BANDWIDTH 230000 in chaos's master
# playlists), yt's 144p video (~80-110 kbit) plus its lowest audio (~50 kbit), tt's H.264 single rung hd (vbitrate
# 1800000 on chaos's rooms)
FALLBACK_RUNG_KBIT = {"twitch": 230.0, "youtube": 130.0, "tiktok": 1800.0}


def _rate(n) -> float | None:
    try:
        return float(n.speed if hasattr(n, "speed") else n["speed"])
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _ts(n) -> float | None:
    try:
        return float(n.timestamp if hasattr(n, "timestamp") else n["timestamp"])
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def above_since(net_labels: list, rung_kbit: float | None, now: float) -> float | None:
    """Start (s) of the current stretch of shaper labels with rate > rung_kbit, or None when the label in force at `now`
    is at or below it, there is none, or the rung is unknown."""
    if not rung_kbit:
        return None
    labs = sorted([n for n in net_labels or [] if _ts(n) is not None and _ts(n) <= now], key=_ts)
    if not labs or (_rate(labs[-1]) or 0) <= rung_kbit:
        return None
    t = _ts(labs[-1])
    for n in reversed(labs):
        if (_rate(n) or 0) <= rung_kbit:
            break
        t = _ts(n)
    return t


def t2_clock(frozen_s: float, now: float, net_labels: list, rung_kbit: float | None) -> tuple[float | None, float | None]:
    """(T2 seconds, the T2 window's start) for a frozen run of frozen_s seconds ending now; (None, None) below the rung."""
    if not frozen_s or frozen_s <= 0:
        return None, None
    a = above_since(net_labels, rung_kbit, now)
    if a is None:
        return None, None
    start = max(now - frozen_s, a)
    return max(0.0, now - start), start


def action(recoveries_used: int) -> str:
    """At T2: 'reenter' while a recovery remains this session, else 'censor'."""
    return "reenter" if recoveries_used < RECOVERIES_PER_SESSION else "censor"


def shaper_at(net_labels: list, now: float) -> dict:
    labs = sorted([n for n in net_labels or [] if _ts(n) is not None and _ts(n) <= now], key=_ts)
    if not labs:
        return {"state": None, "kbit": None}
    n = labs[-1]
    st = n.state if hasattr(n, "state") else n.get("state")
    return {"state": None if st is None else str(st), "kbit": _rate(n)}


# ---------------------------------------------------------------------------------------------------- the lowest rung
def rung_from_master(m3u8: str) -> float | None:
    """Lowest BANDWIDTH (kbit/s) over a master playlist's video variants (audio-only variants skipped)."""
    bws = []
    for attrs in re.findall(r"#EXT-X-STREAM-INF:([^\n]*)", m3u8 or ""):
        if "RESOLUTION=" not in attrs:
            continue
        m = re.search(r"(?:^|,)BANDWIDTH=(\d+)", attrs)
        if m:
            bws.append(int(m.group(1)) / 1000.0)
    return min(bws) if bws else None


def rung_from_tt_sdk(text: str, codec: str | None) -> float | None:
    """Lowest vbitrate (kbit/s) over the room data's renditions of `codec` (h264 / hevc), from sdk_params blocks."""
    import tt_stream
    t = (text or "").replace("\\", "")
    rates = []
    for b in tt_stream._blocks(t):
        vc = re.search(r'"VCodec":"(\w+)"', b)
        vb = re.search(r'"vbitrate":(\d+)', b)
        if not (vc and vb) or int(vb.group(1)) <= 0:
            continue
        if codec is None or tt_stream.CODEC.get(vc.group(1).lower()) == codec:
            rates.append(int(vb.group(1)) / 1000.0)
    return min(rates) if rates else None


def rung_from_yt_formats(formats: list[dict]) -> float | None:
    """The lowest rung as delivered (kbit/s): the lowest adaptive video format's bitrate plus the lowest audio format's
    (yt sends them as separate streams; twitch's master BANDWIDTH already counts audio)."""
    def low(kind):
        r = [f.get("bitrate") / 1000.0 for f in formats or [] if str(f.get("mimeType", "")).startswith(kind)
             and isinstance(f.get("bitrate"), (int, float)) and f["bitrate"] > 0]
        return min(r) if r else None
    v, a = low("video/"), low("audio/")
    return None if v is None else v + (a or 0.0)


def rung_record(platform: str, kbit: float | None, source: str | None, detail=None) -> dict:
    if kbit:
        return {"kbit": round(kbit, 1), "source": source, "detail": detail}
    return {"kbit": FALLBACK_RUNG_KBIT.get(platform), "source": "fallback", "detail": detail}


# page JS (no network traffic): yt's player response, tt's room data and the player's codec decision (localStorage)
YT_FORMATS_JS = """(() => { try {
  const p = document.querySelector('#movie_player');
  const r = (p && p.getPlayerResponse && p.getPlayerResponse()) || window.ytInitialPlayerResponse || {};
  const f = ((r.streamingData || {}).adaptiveFormats || []).map(x => ({mimeType: x.mimeType, bitrate: x.bitrate}));
  return JSON.stringify(f);
} catch (e) { return null; } })()"""
TT_ROOM_JS = """(() => { try {
  let h265 = null;
  try { const a = JSON.parse(localStorage.getItem('webcodecs_ability_key_temporary') || 'null'); if (a) h265 = !!a.isSupportH265; } catch (e) {}
  const txt = Array.from(document.scripts).map(s => s.textContent || '').filter(t => t.includes('sdk_params')).join('\\n');
  return JSON.stringify({h265: h265, text: txt.slice(0, 2000000)});
} catch (e) { return null; } })()"""


def rung_from_page(platform: str, page_value: str | None) -> tuple[float | None, str | None, dict | None]:
    """(kbit, source, detail) from YT_FORMATS_JS / TT_ROOM_JS output."""
    try:
        v = json.loads(page_value) if page_value else None
    except ValueError:
        return None, None, None
    if platform == "youtube" and v:
        return rung_from_yt_formats(v), "yt_player_response", {"formats": len(v)}
    if platform == "tiktok" and v:
        codec = None if v.get("h265") is None else ("hevc" if v["h265"] else "h264")
        return rung_from_tt_sdk(v.get("text") or "", codec), "tt_room_sdk_params", {"codec": codec}
    return None, None, None
