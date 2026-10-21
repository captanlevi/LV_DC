"""stuck.py: the stuck-player rule (LVQoE collection prereg Addendum Q amendments 5-6, 2026-10-18; rushi's question on a
5-minute twitch stall; shagun's tw_1 / tw_27 reads: after a down-switch under starvation the twitch player held ~24 s of
media and never fed the element again, frozen for 10+ min with the shaper back at 2-4x the lowest rung).

- The frozen measure is content.js's lvqoeFrozenS (= check_session.frozen_runs): the backstop watchdog and this rule
  read the same run, so they cannot disagree.
- T2 clock = now - max(the frozen run's start, the start of the current stretch of shaper labels at >= RUNG_MARGIN x
  the platform's lowest-rung bitrate, am. 7). Below that, or with no rung read from the stream (only the fallback:
  t2_rung), there is no T2: a starved player is not a stuck one, and a margin needs a read rung (supervisor 2026-10-18,
  tw_34: censored against twitch's 230 kbit fallback at state 4 while the master had not been read). A session still
  on the fallback at T2_S of frozen time logs t2_skipped_rung_unknown (meta.json t2_skipped).
- First T2 >= T2_S: one re-entry of the same channel / room (recorded as an entry with recovered_frozen_s and its
  window); playback must resume within REENTRY_CONFIRM_S. A second T2, or no resume: player_frozen, censored (time and
  shaper state); the session ends. At most RECOVERIES_PER_SESSION recovery of either kind (tt's player_dead re-entry
  included). Labels are unchanged; the part of the frozen window after the shaper went back above the rung is recorded.

The lowest rung: tt from the room data's sdk_params vbitrate (the played codec's ladder; video only, tt's audio
bitrate is not in the room data), twitch from the HLS master playlist's lowest BANDWIDTH (audio included), yt from the
lowest offered video format's bitrate plus the lowest audio format's; FALLBACK_RUNG_KBIT when none was read (source
"fallback", recorded in meta, never a T2 rung). The shaper's rate is compared as is (no protocol-overhead margin).
"""
from __future__ import annotations

import json
import re

T2_S = 90.0
# Addendum Q am. 7 (levi 2026-10-18): the T2 clock runs only while the shaper is >= this x the lowest rung (protocol
# overhead ~6 %, VBR peaks, tt's audio missing from its rung); yt's state-1 startup stalls stay out of T2
RUNG_MARGIN = 1.5
REENTRY_CONFIRM_S = 60.0
RECOVERIES_PER_SESSION = 1
# a fallback only (source "fallback"): twitch's transcode ladder's 160p30 (BANDWIDTH 230000 in chaos's master
# playlists), yt's 144p video (~80-110 kbit) plus its lowest audio (~50 kbit), tt's H.264 single rung hd (vbitrate
# 1800000 on chaos's rooms)
FALLBACK_RUNG_KBIT = {"twitch": 230.0, "youtube": 130.0, "tiktok": 1800.0}
# the one stated exception to "no T2 on the fallback" (supervisor 2026-10-18): twitch's 160p30 transcode rung, whose
# BANDWIDTH is the same 230000 in every master chaos has read, when the player's own quality menu lists a 160p option
# and the master is not read (source "ladder_160p_transcode")
TWITCH_160P_TRANSCODE_KBIT = 230.0


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


def _t2_rate(n) -> float | None:
    """The rate the T2 clock reads: the sub-step's re-draw (redraw_kbit) where the row has one, so restart §2's walk
    steps leave the stuck rule's calibration as it was (levi on 80af314); else the row's rate (rows before §2)."""
    r = n.redraw_kbit if hasattr(n, "redraw_kbit") else (n.get("redraw_kbit") if isinstance(n, dict) else None)
    return float(r) if r is not None else _rate(n)


def above_since(net_labels: list, rung_kbit: float | None, now: float, margin: float = RUNG_MARGIN) -> float | None:
    """Start (s) of the current stretch of shaper labels with rate (_t2_rate: the sub-step's re-draw) >= margin x
    rung_kbit, or None when the label in force at `now` is below it, there is none, or the rung is unknown."""
    if not rung_kbit:
        return None
    floor = margin * rung_kbit
    labs = sorted([n for n in net_labels or [] if _ts(n) is not None and _ts(n) <= now], key=_ts)
    if not labs or (_t2_rate(labs[-1]) or 0) < floor:
        return None
    t = _ts(labs[-1])
    for n in reversed(labs):
        if (_t2_rate(n) or 0) < floor:
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


def t2_rung(rung: dict | None) -> float | None:
    """The rung (kbit) the T2 margin runs on: a rung read from the stream only; the fallback never starts T2."""
    if not rung or rung.get("source") in (None, "fallback"):
        return None
    return rung.get("kbit")


# levi 10:27 (t58 w4, twitch's state-1 fire): on yt / twitch the stuck-player and ad_frozen clocks hold while the shaper is
# in the lowest states and for a grace after leaving them, as tt's player_dead clock does (browser TT_LOW_STATES /
# TT_STALL_GRACE_S, bd4bebf): a frozen player under those rates is the shaping, not something to intervene on
LOW_HOLD_STATES = frozenset({1, 2, 3, 4})
LOW_HOLD_GRACE_S = 30.0


def low_hold_end(net_labels: list, t: float) -> float | None:
    """The time the low-state hold in force at t ends (the end of the shaper's last stretch in LOW_HOLD_STATES up to t,
    plus LOW_HOLD_GRACE_S; a stretch still running at t ends at t), or None when no such stretch began by t. Held at t
    iff t <= the returned time."""
    labs = sorted([n for n in net_labels or [] if _ts(n) is not None and _ts(n) <= t], key=_ts)
    end = None
    for a, b in zip(labs, labs[1:] + [None]):
        try:
            low = int(a.state if hasattr(a, "state") else a.get("state")) in LOW_HOLD_STATES
        except (TypeError, ValueError):
            low = False
        if low:
            end = (_ts(b) if b is not None else t) + LOW_HOLD_GRACE_S
    return end


def player_error_held(errors: list | None, net_labels: list) -> bool:
    """levi 02:57: the session's aborting player error (the last record with an "abort", else the last) began inside the
    low-state hold (states 1-4 + LOW_HOLD_GRACE_S) at its onset (since, else t_trigger). Unshaped (no label by then), past
    the grace, or no error: False (the accounting as before)."""
    errs = [e for e in errors or [] if isinstance(e, dict)]
    e = next((x for x in reversed(errs) if x.get("abort")), errs[-1] if errs else None)
    t = (e or {}).get("since") if isinstance((e or {}).get("since"), (int, float)) else (e or {}).get("t_trigger")
    if not isinstance(t, (int, float)):
        return False
    he = low_hold_end(net_labels, float(t))
    return he is not None and float(t) <= he


def low_spans(net_labels: list) -> list[tuple[float, float]]:
    """The shaper's stretches in LOW_HOLD_STATES: (first low label's time, the next label's time; inf if none)."""
    labs = sorted([n for n in net_labels or [] if _ts(n) is not None], key=_ts)
    out = []
    for a, b in zip(labs, labs[1:] + [None]):
        try:
            low = int(a.state if hasattr(a, "state") else a.get("state")) in LOW_HOLD_STATES
        except (TypeError, ValueError):
            low = False
        if low:
            end = _ts(b) if b is not None else float("inf")
            if out and out[-1][1] == _ts(a):
                out[-1] = (out[-1][0], end)
            else:
                out.append((_ts(a), end))
    return out


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
