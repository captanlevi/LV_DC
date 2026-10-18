"""check_session.py: post-session acceptance check for collection v2 (LVQoE t35; prereg collection_v2 §2, levi R3/R4).

    python check_session.py <session_dir> [--move-failed] [--max-background-share 0.05]

Writes <session_dir>/check.json and a one-screen check.md. Verdict PASS / FAIL with reasons. With --move-failed a FAIL
moves the session dir to data/_failed/<platform>/ (archive, never delete).

Acceptance is CAPTURE INTEGRITY ONLY (prereg §2, brad R1): nothing that co-varies with impairment filters a session.
  ticks      res_meta present in >= 0.95 of the session's seconds (integrity). `current_time` advancing in >= 0.8 of
             playing ticks is a documented deviation from the prereg's 0.95: it couples to stall detection, and a strict
             value would select on impairment (levi R3c).
  usable     non-ad labelled time, play OR stall, >= 0.6 of the session length (ads by the sensor_v2 span rule)
  shaper     measured downstream <= 1.1 x the set rate in >= 0.9 of episodes
  background downstream bytes on flows whose host (TLS / QUIC SNI, else DNS answers) is not the platform's media host
             <= 0.05 of all downstream bytes; independent of the keylog (a keylog-based share would select clean
             sessions, t23 / t30). The per-episode maximum and the http_v2-based share are reported, not enforced.
  artefacts  no minute with zero client packets while the shaper runs; screenshots present; meta.json complete
  keylog     recorded, never filtered
  frozen     player_frozen (addendum N): the longest stall-only frozen-playhead run (stall label on, non-ad tick, playhead
             within 1 s of the run's first tick) <= FROZEN_PLAYHEAD_MAX_S[platform] (1.2 x the old data's maximum,
             rounded up to the minute). browser.py's watchdog aborts a session at the same T, on the same measure.
  latch      I10 (levi): <= 0.5 of within-stall ticks (previous tick also in stall) with the playhead advancing at
             0.8-1.5 s/s (a stall label that outlives playback), scored with >= 10 such ticks
QA fields for the readings (levi R4): per-signal ad tick counts and ad spans per signal set; Addendum D (twitch: share
of tier changes with a src_changed within ± 2 s; swap-only ad spans, must be 0); Addendum C (realised dwell per shaper
state, in episodes); yt 144 / 240 / 360p shares of quality time (ads masked); ad-action summary; the player pin.
"""
from __future__ import annotations

import argparse
import collections
import csv
import ipaddress
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

CHECKS = {
    "tick_presence_min": 0.95,
    "advancing_share_min": 0.8,
    "usable_share_min": 0.6,
    "shaper_ratio_max": 1.1,
    "shaper_episode_share_min": 0.9,      # reported QA only, never a FAIL (levi addendum G: net_labels are coverage / debug)
    "max_background_share": 0.05,          # EXOGENOUS background over the shaper window (levi addenda F, I)
    "unknown_list_share": 0.02,            # list the top unnamed servers in check.md above this share (addendum I)
    "window_background_flag": 0.25,     # per 60 s wall-clock window: background > 0.25 x its downstream (reported; addendum G)
    "flag_window_s": 60.0,
    "shots_share_min": 0.5,
    "shot_interval_s": 10.0,
}
# addendum N (supervisor 2026-10-17, tw_3 / tw_4): the player froze and never recovered. T per platform = 1.2 x the
# longest stall-only frozen-playhead run in the old data (earth / fire / shadow / water), rounded up to the minute:
# yt 116 s (water/yt_60), twitch 513 s (earth/tw-39), tt 567 s (fire/Tiktok_36). One table, read by check_session's
# FAIL and by browser.py's watchdog; frozen_playhead_run() here and content.js's lvqoeFrozenS compute the same run.
FROZEN_PLAYHEAD_MAX_S = {"youtube": 180, "twitch": 660, "tiktok": 720}
# player_dead (supervisor 2026-10-17, ti_20 / ti_21): same T-table family, a separate reason: readyState 0 with no media
# for this long after the player had played. tt only (browser.py TTRoomWatch aborts on it live). Old tt data: every 0x0 /
# playhead-0 run that recovered was <= 20 s (21 sessions on chaos, 2026-10-17)
PLAYER_DEAD_MAX_S = {"tiktok": 60}
FROZEN_PLAYHEAD_TOL_S = 1.0
# I10 (levi, latched-stall guard; LVQoE scripts/analysis/i10_stall_advance.py, within-stall rule): share of within-stall
# ticks whose playhead advances at a playing rate, scored with >= I10_MIN_STALL_TICKS such ticks; old data max yt / tt /
# twitch 0.0 (bilibili/bili_new_17 0.963, a recorded latch candidate)
STALL_ADVANCING_RATE = (0.8, 1.5)
STALL_ADVANCING_SHARE_MAX = 0.5
I10_MIN_STALL_TICKS = 10
# sensor_v2's span-type ad signals (LVQoE label_policies.SPAN_TYPE_SIGNALS) and its span rule
SPAN_TYPE_SIGNALS = ("duration_finite", "yt_ad_showing", "yt_ad_interrupting", "yt_ad_dom", "tw_ad_dom")
AD_MERGE_GAP_S, AD_PAD_S, SWAP_BOUNDARY_S = 2.0, 1.0, 2.0
# media hosts per platform (LVQoE watcher/lv/constants.py MEDIA_SNI_MP, selection generation 2)
MEDIA_HOSTS = {
    "youtube": [r"(^|\.)googlevideo\.com$"],
    "twitch": [r"(^|\.)ttvnw\.net$", r"(^|\.)live-video\.net$"],
    # t41: chaos ti_1's FLV came from <ip-dashed>.fcdnstatic-intl.com (97 % of bytes); LVQoE MEDIA_SNI_MP gets the same
    "tiktok": [r"(^|\.)pull-[a-z0-9-]+\.(tiktokcdn|ttlivecdn)\.com$", r"(^|\.)fcdnstatic-intl\.com$"],
    "bilibili": [r"(^|\.)bilivideo\.(com|cn)$", r"(^|\.)hdslb\.com$"],
}
# background categories (copied from LVQoE scripts/analysis/t40_old_background.py, master 2a6e771; keep in sync).
# levi prereg addendum I: only EXOGENOUS background (Chrome's own and the OS's downloads, and unnamed flows) is enforced;
# platform_page (first-party non-media: page, chat websockets, images) and other are reported
CHROME_SERVICE = re.compile(
    r"optimizationguide|(^|\.)update\.googleapis\.com$|(^|\.)gvt1\.com$|(^|\.)gvt2\.com$|^clients\d*\.google\.com$|"
    r"clients\d*\.googleusercontent\.com$|safebrowsing|content-autofill|clientservices\.googleapis\.com$|"
    r"(^|\.)dl\.google\.com$|edgedl\.|^accounts\.google\.com$|^android\.clients\.google\.com$|^mtalk\.google\.com$|"
    r"^chromewebstore\.googleapis\.com$|^safebrowsingohttpgateway|^beacons\d*\.gvt\d\.com$")
OS_SERVICE = re.compile(r"(^|\.)(ubuntu\.com|canonical\.com|snapcraft\.io|snapcraftcontent\.com|launchpad\.net|"
                        r"launchpadcontent\.net|fwupd\.org|debian\.org)$")
PLATFORM_PAGE = {
    "youtube": re.compile(r"(^|\.)(youtube\.com|ytimg\.com|ggpht\.com|youtube-nocookie\.com|youtubei\.googleapis\.com)$"),
    "twitch": re.compile(r"(^|\.)(twitch\.tv|jtvnw\.net|twitchcdn\.net|twitchsvc\.net|ext-twitch\.tv)$"),
    "tiktok": re.compile(r"(^|\.)(tiktok\.com|tiktokv\.com|tiktokv\.us|tiktokcdn\.com|tiktokcdn-us\.com|ttwstatic\.com|"
                         r"ibytedtos\.com|byteoversea\.com|ttlivecdn\.com|tiktokw\.us)$"),
    "bilibili": re.compile(r"(^|\.)(bilibili\.com|bilibili\.cn|biliapi\.(com|net)|hdslb\.net|biliimg\.com)$"),
}
EXOGENOUS = ("chrome_service", "os_service", "unknown")
LOW_STATE_MAX = 3   # LV_DC utils/netStat._LOW_STATE_THRESHOLD: states 1-3 are the stall-risk "low" states


def flow_category(names: set, platform: str) -> str:
    """media / chrome_service / os_service / platform_page / other, or unknown when the flow has no name."""
    names = {n for n in names if n}
    if not names:
        return "unknown"
    if any(re.search(pt, n) for n in names for pt in MEDIA_HOSTS.get(platform, [])):
        return "media"
    if any(CHROME_SERVICE.search(n) for n in names):
        return "chrome_service"
    if any(OS_SERVICE.search(n) for n in names):
        return "os_service"
    pp = PLATFORM_PAGE.get(platform)
    if pp is not None and any(pp.search(n) for n in names):
        return "platform_page"
    return "other"


MEDIA_URL_PATTERNS = ("/videoplayback", ".ts", ".m4s", ".flv", "/v1/segment/")
META_REQUIRED = ("lvdc_commit", "extension_version", "chrome", "shaper", "channel_url", "hostname", "ntp_offset_start")
TSHARK = shutil.which("tshark") or "tshark"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entries as E   # noqa: E402  (session_class, entries, dead-player run; levi 2026-10-17)
import stream_regime as SR   # noqa: E402  (ladder / codec / player-size flags; supervisor 2026-10-18)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _true(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "1.0")


# ---------------------------------------------------------------- labels

def read_labels(sd: Path) -> list[dict]:
    p = sd / "labels.csv"
    if not p.exists():
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def ad_spans(meta: list[dict], signal_set=SPAN_TYPE_SIGNALS) -> tuple[list[list[float]], int]:
    """sensor_v2's rule on res_meta ticks: span-type ticks (+ src_changed within 2 s of one), merged across gaps
    <= 2 s, padded ± 1 s. Returns (spans in seconds, number of spans with no span-type tick = swap-only)."""
    allowed = set(signal_set)
    ad_t = sorted(_f(r["ts"]) / 1000 for r in meta if allowed & set((r.get("ad_signals") or "").split("|")))
    sw_t = [_f(r["ts"]) / 1000 for r in meta if _true(r.get("src_changed"))]
    pts = sorted(ad_t + [t for t in sw_t if ad_t and min(abs(t - a) for a in ad_t) <= SWAP_BOUNDARY_S])
    spans: list[list[float]] = []
    for t in pts:
        if spans and t - spans[-1][1] <= AD_MERGE_GAP_S:
            spans[-1][1] = t
        else:
            spans.append([t, t])
    swap_only = sum(1 for lo, hi in spans if not any(lo <= a <= hi for a in ad_t))
    return [[lo - AD_PAD_S, hi + AD_PAD_S] for lo, hi in spans], swap_only


def frozen_playhead_run(rows: list[dict]) -> float:
    """Longest stall-only frozen-playhead run, in seconds (addendum N): over res_meta ticks with the stall label on
    (stall ... start / video_start), not an ad tick and with a parseable current_time, a run starts when there is none
    open or the playhead has moved more than FROZEN_PLAYHEAD_TOL_S from the run's first tick; any other tick ends it."""
    stall, t0, ct0, longest = False, None, None, 0.0
    for r in rows:
        ty = r.get("type")
        if ty == "stall":
            stall = True
        elif ty in ("start", "video_start"):
            stall = False
        if ty != "res_meta":
            continue
        t, ct = _f(r.get("ts")), _f(r.get("current_time"))
        if not stall or _true(r.get("is_ad")) or t is None or ct is None:
            t0 = None
            continue
        if t0 is None or abs(ct - ct0) > FROZEN_PLAYHEAD_TOL_S:
            t0, ct0 = t, ct
        longest = max(longest, (t - t0) / 1000.0)
    return longest


PAUSED_WHILE_PLAYING_MAX = 0.05     # levi 2026-10-17 (tw_10): reported; above it the session is suspicious


def paused_while_playing_share(rows: list[dict]) -> float | None:
    """Playing-labelled, non-ad res_meta ticks whose element was paused, over playing-labelled non-ad ticks (raw
    `paused` column, sessions from a99384f on; None before)."""
    stall, n, k = False, 0, 0
    for r in rows:
        ty = r.get("type")
        if ty == "stall":
            stall = True
        elif ty in ("start", "video_start"):
            stall = False
        if ty != "res_meta" or stall or _true(r.get("is_ad")) or r.get("paused") in (None, "", "None"):
            continue
        n += 1
        k += _true(r.get("paused"))
    return k / n if n else None


def stall_advancing_share(rows: list[dict]) -> float | None:
    """I10: over res_meta ticks in stall whose previous res_meta tick was also in stall (the rate is measured inside
    the stall, so a sub-second stall's single tick does not read the playing tick's advance; levi 2026-10-17), the
    share whose playhead advanced at a rate inside STALL_ADVANCING_RATE s/s; None (unscored) with fewer than
    I10_MIN_STALL_TICKS such ticks."""
    stall, prev, prev_stall, n, adv, has_ct = False, None, False, 0, 0, False
    for r in rows:
        ty = r.get("type")
        if ty == "stall":
            stall = True
        elif ty in ("start", "video_start"):
            stall = False
        if ty != "res_meta":
            continue
        t, ct = _f(r.get("ts")), _f(r.get("current_time"))
        if stall and prev_stall:
            n += 1          # every scored tick counts; a tick with no rate (no previous tick / current_time) is not advancing
            has_ct = has_ct or ct is not None
            if prev is not None and t is not None and ct is not None and t > prev[0]:
                lo, hi = STALL_ADVANCING_RATE
                adv += lo <= (ct - prev[1]) / ((t - prev[0]) / 1000.0) <= hi
        prev = (t, ct) if t is not None and ct is not None else None
        prev_stall = stall
    return adv / n if n >= I10_MIN_STALL_TICKS and has_ct else None


def shot_effects(rows: list[dict], sd: Path, near_s: float = 1.0) -> dict | None:
    """Does a screenshot disturb playback? (rushi saw the screen flicker; supervisor 2026-10-17.) res_meta ticks within
    near_s of a shot vs the rest: dropped frames per tick (the content.js dropped counter's increments) and ticks whose
    client size is off the session's modal size. Reported, never a FAIL."""
    idx = sd / "shots" / "index.jsonl"
    if not idx.exists():
        return None
    # cadence shots only (2026-10-17): event shots are triggered by tier changes and ad starts, which drop frames by
    # themselves, so pooling them read as a capture effect (tw_11: cadence 0.010 vs 0.013 elsewhere, event shots 0.469);
    # the shot's start (t_decide) when recorded, else its write time
    recs = [json.loads(ln) for ln in idx.read_text().splitlines() if ln.strip()]
    shots = sorted(x.get("t_decide") or x["ts"] for x in recs if (x.get("reason") or "cadence") == "cadence")
    events = sorted(x.get("t_decide") or x["ts"] for x in recs if (x.get("reason") or "cadence") != "cadence")
    meta = [r for r in rows if r.get("type") == "res_meta"]
    ts = [(_f(r.get("ts")) or 0) / 1000.0 for r in meta]
    if len(meta) < 2 or not shots:
        return None
    sizes = [(r.get("client_w"), r.get("client_h")) for r in meta]
    mode = collections.Counter(sizes).most_common(1)[0][0]
    dropped = [_f(r.get("dropped")) or 0.0 for r in meta]
    import bisect
    near = []
    for t in ts:
        j = bisect.bisect_left(shots, t)
        near.append(any(0 <= k < len(shots) and abs(shots[k] - t) <= near_s for k in (j - 1, j)))
    inc = [max(0.0, dropped[i] - dropped[i - 1]) for i in range(1, len(meta))]
    nd = [x for x, n in zip(inc, near[1:]) if n]
    fd = [x for x, n in zip(inc, near[1:]) if not n]
    ev_near = [False] * len(ts)
    for i, t in enumerate(ts):
        j = bisect.bisect_left(events, t)
        ev_near[i] = any(0 <= k < len(events) and abs(events[k] - t) <= near_s for k in (j - 1, j))
    en = [x for x, n in zip(inc, ev_near[1:]) if n]
    return {"near_s": near_s, "shots": "cadence", "ticks_near": sum(near), "ticks_elsewhere": len(near) - sum(near),
            "event_shots": len(events), "dropped_per_tick_near_event_shots": (sum(en) / len(en)) if en else None,
            "dropped_per_tick_near": (sum(nd) / len(nd)) if nd else None,
            "dropped_per_tick_elsewhere": (sum(fd) / len(fd)) if fd else None,
            "client_size_off_mode_near": sum(1 for s_, n in zip(sizes, near) if n and s_ != mode),
            "client_size_off_mode_elsewhere": sum(1 for s_, n in zip(sizes, near) if not n and s_ != mode)}


def label_checks(rows: list[dict], platform: str, reentries: list[dict] | None = None) -> dict:
    meta = [r for r in rows if r.get("type") == "res_meta"]
    if len(meta) < 2:
        return {"n_res_meta": len(meta)}
    ts = [_f(r["ts"]) / 1000.0 for r in meta]
    t0, t1 = min(ts), max(ts)
    dur = t1 - t0
    seconds = max(1, int(dur) + 1)
    present = len({int(t - t0) for t in ts})
    has_v2 = "ad_signals" in meta[0]
    spans, swap_only = ad_spans(meta) if has_v2 else ([], 0)
    in_ad = [any(lo <= t <= hi for lo, hi in spans) for t in ts]
    # stall state from the event stream
    stalled, state = [], False
    for r in rows:
        if r.get("type") == "stall":
            state = True
        elif r.get("type") in ("start", "video_start"):
            state = False
        elif r.get("type") == "res_meta":
            stalled.append(state)
    h = [int(_f(r.get("video_h")) or 0) for r in meta]
    ct = [_f(r.get("current_time")) for r in meta]
    adv = [b is not None and a is not None and b > a for a, b in zip(ct, ct[1:])]
    adv_play = [a for a, s, ad in zip(adv, stalled[1:], in_ad[1:]) if not s and not ad]
    usable = sum(1 for hh, s, ad in zip(h, stalled, in_ad) if not ad and (s or hh > 0))
    tiers = [hh for hh, s, ad in zip(h, stalled, in_ad) if not ad and not s and hh > 0]
    mix = collections.Counter(tiers)
    nq = max(1, len(tiers))
    changes = [(t, a, b) for t, (a, b) in zip(ts[1:], zip(tiers, tiers[1:]))]  # approximate times (dense ticks)
    # Addendum D (a): tier changes with a src_changed within ± 2 s (non-ad play ticks, consecutive)
    play_idx = [i for i, (hh, s, ad) in enumerate(zip(h, stalled, in_ad)) if not ad and not s and hh > 0]
    sw_t = [t for t, r in zip(ts, meta) if _true(r.get("src_changed"))]
    tier_changes = [ts[j] for i, j in zip(play_idx, play_idx[1:]) if j == i + 1 and h[i] != h[j]]
    near = sum(1 for t in tier_changes if any(abs(t - s) <= SWAP_BOUNDARY_S for s in sw_t))
    per_signal = collections.Counter(s for r in meta for s in (r.get("ad_signals") or "").split("|") if s)
    dp = E.dead_player(rows, reentries)   # levi 2026-10-18: a run recovered by the re-entry is reported, not the verdict
    out = {
        "n_res_meta": len(meta), "duration_s": dur,
        "tick_presence": present / seconds,
        "advancing_share_while_playing": (sum(adv_play) / len(adv_play)) if adv_play else None,
        "v2_columns": has_v2,
        "usable_share": usable / seconds,
        "ad_ticks_per_signal": dict(per_signal),
        "ad_spans_span_type_set": len(spans), "ad_span_seconds": sum(hi - lo for lo, hi in spans),
        "ad_spans_per_signal": {s: len(ad_spans(meta, (s,))[0]) for s in SPAN_TYPE_SIGNALS} if has_v2 else {},
        "swap_only_ad_spans": swap_only,
        "src_changed_ticks": len(sw_t),
        "stall_share": sum(stalled) / len(stalled) if stalled else None,
        "frozen_playhead_max_s": frozen_playhead_run(rows),
        "paused_while_playing_share": paused_while_playing_share(rows),
        "frozen_playhead_T_s": FROZEN_PLAYHEAD_MAX_S.get(platform),
        "dead_player_max_s": dp["max_s"], "dead_player_max_all_s": dp["max_all_s"],
        "recovered_dead": dp["recovered"], "recovered_dead_runs": dp["recovered_runs"],
        "dead_player_T_s": PLAYER_DEAD_MAX_S.get(platform),
        "stall_advancing_share": stall_advancing_share(rows),
        "tier_mix_quality_time": {str(k): v / nq for k, v in sorted(mix.items())},
        "tier_changes": len(tier_changes),
        "tier_changes_per_min": len(tier_changes) / (dur / 60.0) if dur > 0 else None,
        "tier_changes_with_swap_within_2s_share": (near / len(tier_changes)) if tier_changes else None,
        # t37: content.js login-wall flags (columns absent on pre-t37 extensions → None)
        "login_wall_ticks": sum(_true(r.get("login_wall")) for r in meta) if "login_wall" in meta[0] else None,
        "login_wall_text_ticks": sum(_true(r.get("login_wall_text")) for r in meta) if "login_wall_text" in meta[0] else None,
    }
    out["quality_countdown_ticks"] = (sum(_true(r.get("quality_countdown")) for r in meta)
                                      if "quality_countdown" in meta[0] else None)
    if platform == "youtube":
        out["yt_low_tier_shares"] = {f"{q}p": mix.get(q, 0) / nq for q in (144, 240, 360)}
    if platform == "tiktok":
        out.update(tt_rooms(meta, ts, h, ct))
    return out


TT_ROOM_RE = re.compile(r"tiktok\.com/@([^/?#]+)/live")
TT_CAP_WINDOW_S = (55.0, 75.0)   # laptop run3 / run4: the logged-out countdown reset at 64.7 / 62.0 s into the visit


def tt_room_of(url: str | None) -> str | None:
    m = TT_ROOM_RE.search(url or "")
    return m.group(1) if m else None


def tt_rooms(meta: list[dict], ts: list[float], h: list[int], ct: list) -> dict:
    """tt (t37): the room per tick (tab URL), room changes, explore-page ticks, and per room visit the source resets
    (dims 0 or current_time back > 1 s) in TT_CAP_WINDOW_S after which the rendition is lower (the logged-out
    countdown's signature; reported, not enforced: the quality_countdown ticks are the FAIL)."""
    rooms = [tt_room_of(r.get("url")) for r in meta]
    visits, start = [], 0
    for i in range(1, len(rooms) + 1):
        if i == len(rooms) or rooms[i] != rooms[start]:
            visits.append((start, i))
            start = i
    cap_resets = []
    for a, b in visits:
        if rooms[a] is None:
            continue
        for i in range(a + 1, b):
            reset = h[i] == 0 or (ct[i] is not None and ct[i - 1] is not None and ct[i] < ct[i - 1] - 1)
            if not reset or not (TT_CAP_WINDOW_S[0] <= ts[i] - ts[a] <= TT_CAP_WINDOW_S[1]):
                continue
            before = next((h[j] for j in range(i - 1, a - 1, -1) if h[j] > 0), 0)
            after = next((h[j] for j in range(i, b) if h[j] > 0), 0)
            if 0 < after < before:
                cap_resets.append({"room": rooms[a], "t_into_visit_s": round(ts[i] - ts[a], 1), "h": [before, after]})
                break
    return {
        "tt_room_ticks": dict(collections.Counter(r for r in rooms if r)),
        "tt_explore_ticks": sum(1 for r in meta if (r.get("url") or "").split("?")[0].rstrip("/") ==
                                "https://www.tiktok.com/live"),
        "tt_room_changes": sum(1 for x, y in zip(rooms, rooms[1:]) if x and y and x != y),
        "tt_room_visits": [{"room": rooms[a], "start_ts": ts[a], "end_ts": ts[b - 1]} for a, b in visits if rooms[a]],
        "tt_cap_resets_55_75s": cap_resets,
    }


TT_FLV_RE = re.compile(r"/stream-(\d+)_([A-Za-z0-9]+)\.flv")


def tt_flv_tokens(sd: Path, visits: list[dict]) -> dict:
    """tt (t37): FLV rendition tokens (stream-<id>_<token>.flv, e.g. ld5 / sd5 / hd560 / uhd560 / or4) per room, from
    http_v2 request URLs, assigned to the room visit their request_start falls in; consecutive repeats collapsed."""
    p = sd / "http_v2.json"
    if not p.exists() or p.stat().st_size < 10:
        return {}
    reqs = []
    for recs in json.load(open(p)).values():
        for r in (recs if isinstance(recs, list) else []):
            m = TT_FLV_RE.search(r.get("request_url") or "")
            if m and r.get("request_start") is not None:
                reqs.append((float(r["request_start"]), m.group(1), m.group(2)))
    out: dict[str, list[str]] = {}
    for t, stream, tok in sorted(reqs):
        room = next((v["room"] for v in visits if v["start_ts"] - 5 <= t <= v["end_ts"] + 1), "(no room tick)")
        seq = out.setdefault(room, [])
        if not seq or seq[-1] != tok:
            seq.append(tok)
    return out


# ---------------------------------------------------------------- traffic

def packets(pcap: Path):
    """Per packet: time, src, dst, sport, dport, proto, length, sni, dns names, dns addresses (tshark fields)."""
    cmd = [TSHARK, "-r", str(pcap), "-T", "fields", "-E", "separator=\t", "-E", "aggregator=|",
           "-e", "frame.time_epoch", "-e", "ip.src", "-e", "ip.dst", "-e", "ipv6.src", "-e", "ipv6.dst",
           "-e", "tcp.srcport", "-e", "tcp.dstport", "-e", "udp.srcport", "-e", "udp.dstport", "-e", "frame.len",
           "-e", "tls.handshake.extensions_server_name", "-e", "dns.qry.name", "-e", "dns.a", "-e", "dns.aaaa"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in proc.stdout:
        f = line.rstrip("\n").split("\t")
        f += [""] * (14 - len(f))
        t, ln = _f(f[0]), _f(f[9])
        src, dst = (f[1] or f[3]), (f[2] or f[4])
        if f[5]:
            sp, dp, pr = f[5], f[6], "TCP"
        elif f[7]:
            sp, dp, pr = f[7], f[8], "UDP"
        else:
            continue
        if t is None or ln is None or not src or not dst:
            continue
        yield (t, src, dst, sp, dp, pr, ln, f[10], f[11], f[12] + ("|" + f[13] if f[13] else ""))
    proc.wait()


def client_ips(pkts) -> set:
    """The capture host's own addresses, one per family: the top source of packets to server ports 443 / 80 (and the
    top destination of packets from them). The old rule (most common private address) missed IPv6: a global v6
    address is not private, so video over IPv6 was never counted as ours (chaos yt pilot: background 1.000)."""
    c4, c6 = collections.Counter(), collections.Counter()
    for p in pkts:
        if p[4] in ("443", "80"):
            ip = p[1]
        elif p[3] in ("443", "80"):
            ip = p[2]
        else:
            continue
        (c6 if ":" in ip else c4)[ip] += 1
    out = {c.most_common(1)[0][0] for c in (c4, c6) if c}
    if not out:   # no web traffic at all: fall back to the most common private address
        c = collections.Counter(ip for p in pkts for ip in (p[1], p[2]) if _private(ip))
        out = {c.most_common(1)[0][0]} if c else set()
    return out


def _private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def nc_flow_names(sd: Path, pcap: Path) -> tuple[dict, str]:
    """Flow names from network_core's flow table (the LVQoE pipeline's own SNI / QUIC Initial / DNS attribution,
    independent of the keylog and of the box's tshark version), keyed (client, cport, server, sport, proto).
    Written to <session>/check_nc/ (the pipeline's own merged*.csv are produced later, elsewhere). Returns
    ({}, reason) when network_core is unavailable or fails; check_session then falls back to tshark's names."""
    try:
        from network_core.pcap import process_pcap
    except Exception as e:
        return {}, f"network_core unavailable ({type(e).__name__})"
    out_dir = sd / "check_nc"
    flows, pkts = out_dir / "mergedFlows.csv", out_dir / "mergedPackets.csv"
    try:
        if not flows.exists():
            out_dir.mkdir(exist_ok=True)
            process_pcap(str(pcap), str(flows), str(pkts))
            try:
                from network_core.finalize import finalize
                finalize(flows, Path(str(flows) + ".dns_obs.csv"), keep_dns_obs=True)
            except Exception:
                pass   # DNS names stay empty; SNI still names TLS / QUIC flows
        names = {}
        with open(flows) as fh:
            for r in csv.DictReader(fh):
                try:
                    a, b, pr = r["FiveTuple"].split("->")[:3]
                    cip, cport = a.rsplit(":", 1)
                    sip, sport = b.rsplit(":", 1)
                except ValueError:
                    continue
                proto = "TCP" if "6)" in pr and "17)" not in pr else "UDP" if "17)" in pr else pr
                name = next((v for v in (r.get("SNI"), r.get("DNSName")) if v and v.lower() != "nan"), "")
                if name:
                    names[(cip.strip("[]"), cport, sip.strip("[]"), sport, proto)] = name.lower().split("|")[0]
        return names, f"network_core ({len(names)} named flows)"
    except Exception as e:
        return {}, f"network_core failed ({type(e).__name__}: {str(e)[:120]})"


def http_media_flows(sd: Path) -> set:
    p = sd / "http_v2.json"
    if not p.exists() or p.stat().st_size < 10:
        return set()
    keys = set()
    for key, recs in json.load(open(p)).items():
        if any(any(m in (r.get("request_url") or "").split("?")[0] for m in MEDIA_URL_PATTERNS)
               for r in (recs if isinstance(recs, list) else [])):
            try:
                a, b = key.split("->")
                sip, sport = a.split("$")
                dip, dport, proto = b.split("$")
                keys.add((sip, sport, dip, dport, proto))
            except ValueError:
                pass
    return keys


def traffic_checks(sd: Path, platform: str) -> dict:
    pcap = sd / "merged.pcap"
    if not pcap.exists():
        return {"pcap": False}
    pk = list(packets(pcap))
    if not pk:
        return {"pcap": True, "packets": 0}
    mine = client_ips(pk)
    me = ",".join(sorted(mine))
    t0, t1 = min(p[0] for p in pk), max(p[0] for p in pk)
    nc_names, nc_note = nc_flow_names(sd, pcap)
    # host per flow: network_core's name (SNI / QUIC Initial / DNS), else tshark's SNI on the client's handshake,
    # else tshark's DNS answers for the server address
    ip_names: dict[str, set] = collections.defaultdict(set)
    for p in pk:
        if p[8] and p[9]:
            for addr in p[9].split("|"):
                if addr:
                    for name in p[8].split("|"):
                        ip_names[addr].add(name.lower())
    flow_sni: dict[tuple, str] = {}
    for p in pk:
        if p[7] and p[1] in mine:
            flow_sni[(p[1], p[3], p[2], p[4], p[5])] = p[7].split("|")[0].lower()
    name_src = collections.Counter()

    def classify(server, key):
        if key in nc_names:
            names, src = {nc_names[key]}, "network_core"
        elif key in flow_sni:
            names, src = {flow_sni[key]}, "tshark_sni"
        elif ip_names.get(server):
            names, src = ip_names[server], "tshark_dns"
        else:
            names, src = set(), "none"
        name_src[src] += 1
        cat = flow_category(names, platform)
        # an unnamed flow is identified by its server address (a missed video CDN shows up here: addendum I, brad)
        return cat, (sorted(names)[0] if names else f"{key[2]}:{key[3]}/{key[4]}")

    media_cache: dict[tuple, tuple] = {}
    down = []
    for p in pk:
        if p[2] not in mine:
            continue
        key = (p[2], p[4], p[1], p[3], p[5])                  # client-side 5-tuple of the flow
        if key not in media_cache:
            media_cache[key] = classify(p[1], key)
        cat, host = media_cache[key]
        down.append((p[0], p[6], cat == "media", key, cat, host))
    flow_bytes = collections.Counter()
    for d in down:
        flow_bytes[d[3]] += d[1]
    down_total = sum(d[1] for d in down)
    down_media = sum(d[1] for d in down if d[2])
    http_media = http_media_flows(sd)
    down_http_media = sum(d[1] for d in down if d[3] in http_media)
    # per net_labels episode: shaper binding and background share
    eps, binds = [], []
    nl = sd / "net_labels.txt"
    lab = [json.loads(l) for l in nl.read_text().splitlines() if l.strip()] if nl.exists() else []
    for a, b in zip(lab, lab[1:] + [{"timestamp": t1}]):
        lo, hi = a["timestamp"], b["timestamp"]
        if hi - lo < 10:
            continue
        seg = [d for d in down if lo <= d[0] < hi]
        byts = sum(d[1] for d in seg)
        kbit = byts * 8 / 1000 / (hi - lo)
        ratio = kbit / a["speed"] if a.get("speed") else None
        eps.append({"state": a.get("state"), "set_kbit": a.get("speed"), "measured_kbit": kbit, "ratio": ratio,
                    "background_share": (1 - sum(d[1] for d in seg if d[2]) / byts) if byts else None})
        binds.append(ratio is not None and ratio <= CHECKS["shaper_ratio_max"])
    # capture gaps: minutes with zero client packets while the shaper runs
    gaps = []
    if lab:
        s0, s1 = lab[0]["timestamp"], t1
        have = {int((p[0] - s0) // 60) for p in pk if s0 <= p[0] <= s1 and (p[1] in mine or p[2] in mine)}
        gaps = [m for m in range(int((s1 - s0) // 60)) if m not in have]
    # Addendum C (QA only; addendum G: net_labels are coverage / debug): realised dwell per state. net_labels rows are
    # SUB-STEPS of the 60 s episodes (12-30 s each), so a run of same-state rows is measured in seconds and reported
    # in episodes as seconds / 60 (levi: yt_1 showed a "run of 19" rows under cap 6); plus the longest run of
    # consecutive low-state (<= LOW_STATE_MAX) time, against the session's low-state cap
    dwell_s = collections.defaultdict(list)
    low_runs, low_acc = [], 0.0
    run_state, run_s = None, 0.0
    for i, a in enumerate(lab):
        end = lab[i + 1]["timestamp"] if i + 1 < len(lab) else t1
        dur = max(0.0, end - a["timestamp"])
        st = a.get("state")
        if st == run_state:
            run_s += dur
        else:
            if run_state is not None:
                dwell_s[run_state].append(run_s)
            run_state, run_s = st, dur
        if st is not None and str(st).isdigit() and int(st) <= LOW_STATE_MAX:
            low_acc += dur
        elif low_acc:
            low_runs.append(low_acc)
            low_acc = 0.0
    if run_state is not None:
        dwell_s[run_state].append(run_s)
    if low_acc:
        low_runs.append(low_acc)
    dwell = {k: [round(x / 60.0, 1) for x in v] for k, v in dwell_s.items()}
    bg_eps = [e["background_share"] for e in eps if e["background_share"] is not None]
    # the enforced share covers the shaper-running window (first episode to the end of the capture): the labelled
    # collection. Before it, Chrome starts and the page loads cold (yt: ~10 MB from www.youtube.com in the first 10 s,
    # laptop t37), which under matrix A alone would be >= 0.05 of a 21-min session's media bytes. The whole-capture
    # share is reported.
    w0 = lab[0]["timestamp"] if lab else t0
    win = [d for d in down if d[0] >= w0]
    # levi addenda G / I (reported, not a FAIL): 60 s wall-clock windows of the shaped capture whose EXOGENOUS
    # background bytes exceed 0.25 x the window's downstream bytes (independent of net_labels' set rates)
    fw = CHECKS["flag_window_s"]
    wtot, wexo = collections.Counter(), collections.Counter()
    for d in win:
        k = int((d[0] - w0) // fw)
        wtot[k] += d[1]
        if d[4] in EXOGENOUS:
            wexo[k] += d[1]
    heavy_windows = [{"minute": k, "exogenous_of_downstream": round(wexo[k] / wtot[k], 3)} for k in sorted(wtot)
                     if wtot[k] and wexo[k] > CHECKS["window_background_flag"] * wtot[k]]
    win_total = sum(d[1] for d in win)
    win_media = sum(d[1] for d in win if d[2])
    cat_bytes, host_bytes = collections.Counter(), collections.Counter()
    for d in win:
        cat_bytes[d[4]] += d[1]
        if d[4] != "media":
            host_bytes[(d[5] or "(unnamed)", d[4])] += d[1]
    exo_bytes = sum(cat_bytes[c] for c in EXOGENOUS)
    v6_media = sum(d[1] for d in win if d[2] and ":" in d[3][2])
    v6_all = sum(d[1] for d in win if ":" in d[3][2])
    return {
        "pcap": True, "pcap_bytes": pcap.stat().st_size, "minutes": (t1 - t0) / 60.0, "client_ip": me,
        # enforced (FAIL > max_background_share): exogenous background over the shaper window (addendum I)
        "exogenous_share": (exo_bytes / win_total) if win_total else None,
        # reported: all non-media background, per category, and the top background hosts
        "background_share": (1 - win_media / win_total) if win_total else None,
        "category_shares": {c: round(b / win_total, 4) for c, b in cat_bytes.most_common()} if win_total else {},
        "background_hosts_top": [{"host": h, "category": c, "share": round(b / win_total, 4), "mb": round(b / 1e6, 3)}
                                 for (h, c), b in host_bytes.most_common(12)] if win_total else [],
        # addendum I (brad): with an unknown share above UNKNOWN_LIST_SHARE, the top unnamed servers by bytes
        "unknown_hosts_top": [{"server": h, "share": round(b / win_total, 4), "mb": round(b / 1e6, 3)}
                              for (h, c), b in host_bytes.most_common() if c == "unknown"][:12]
                             if win_total and cat_bytes["unknown"] > CHECKS["unknown_list_share"] * win_total else [],
        "background_share_whole_capture": (1 - down_media / down_total) if down_total else None,
        "pre_shaper_downstream_mb": (down_total - win_total) / 1e6,
        "background_share_episode_max": max(bg_eps) if bg_eps else None,
        "background_heavy_windows": heavy_windows,
        "background_share_http_v2": (1 - down_http_media / down_total) if down_total and http_media else None,
        "flows_by_sni": sum(1 for k in media_cache if k in flow_sni), "flows": len(media_cache),
        # attribution diagnostics: where flow names came from, and the largest flows with their names
        "name_source": nc_note, "flows_named_by": dict(name_src),
        "top_flows": [{"flow": f"{k[2]}:{k[3]}/{k[4]}", "host": media_cache[k][1], "category": media_cache[k][0],
                       "mb": round(b / 1e6, 3)} for k, b in flow_bytes.most_common(8)],
        "episodes": eps, "shaper_bind_share": (sum(binds) / len(binds)) if binds else None,
        "capture_gap_minutes": gaps,
        "dwell_episodes_per_state": {k: v for k, v in sorted(dwell.items(), key=lambda kv: int(kv[0]))},
        "dwell_s_per_state": {k: [round(x) for x in v] for k, v in sorted(dwell_s.items(), key=lambda kv: int(kv[0]))},
        "low_run_max_episodes": round(max(low_runs) / 60.0, 1) if low_runs else 0.0,
        # IPv6 (levi / supervisor, t37): old data is IPv4-only; a selected media flow over IPv6 FAILs (ipv6_media)
        "ipv6_media_share": (v6_media / win_media) if win_media else None,
        "ipv6_downstream_share": (v6_all / win_total) if win_total else None,
    }


# ---------------------------------------------------------------- verdict

def meta_check(sd: Path) -> tuple[dict, list[str]]:
    p = sd / "meta.json"
    if not p.exists():
        return {}, ["meta.json missing"]
    m = json.loads(p.read_text())
    missing = [k for k in META_REQUIRED if m.get(k) in (None, "", {}, [])]
    if isinstance(m.get("chrome"), dict) and not m["chrome"].get("browser"):
        missing.append("chrome.browser")
    if isinstance(m.get("lvdc_commit"), dict) and not m["lvdc_commit"].get("sha"):
        missing.append("lvdc_commit.sha")
    if isinstance(m.get("ntp_offset_start"), dict) and not m["ntp_offset_start"].get("raw"):
        missing.append("ntp_offset_start.raw")
    pt = m.get("profile_template") or {}
    if pt.get("mode") != "clone" or not pt.get("sha256"):
        missing.append("profile_template (clone + sha; t37)")
    if m.get("platform") in ("youtube", "tiktok", "twitch"):
        # t38 / addendum M: the matrix id and the low-state cap
        sh = m.get("shaper") or {}
        missing += [f"shaper.{k}" for k in ("matrix_id", "low_state_cap") if sh.get(k) in (None, "")]
    if m.get("platform") == "tiktok" and m.get("login_state") in (None, ""):
        missing.append("login_state (addendum M (a))")
    if m.get("platform") == "youtube":
        # G5 (levi R2): the channel handle / type from the list
        missing += [k for k in ("channel_handle", "channel_type") if m.get(k) in (None, "")]
    return m, missing


def verdict(lab: dict, net: dict, shots: dict, meta_missing: list[str], max_bg: float,
            abort_reason: str | None = None) -> tuple[str, list[str]]:
    why = []
    if abort_reason == "login_wall" or lab.get("login_wall_ticks"):
        why.append("login_wall")   # t37: the platform's logged-out wall stopped playback; the session was aborted
    elif abort_reason == "player_frozen":
        why.append("player_frozen (watchdog abort, addendum N)")
    elif abort_reason == "room_ended":
        why.append("room_ended (tt: the room died mid-session; browser.py TTRoomWatch)")
    elif abort_reason == "player_dead":
        why.append("player_dead (tt: readyState 0 with no media for T; browser.py TTRoomWatch)")
    elif abort_reason:
        why.append(f"aborted: {abort_reason}")
    if lab.get("quality_countdown_ticks"):
        # t37: the tt logged-out quality countdown was on screen: the profile is not logged in, quality gets capped
        why.append(f"tt_logged_out_cap (quality countdown on {lab['quality_countdown_ticks']} ticks)")
    if not net.get("pcap") or not net.get("packets", 1):
        why.append("no pcap / no packets")
    if not net.get("ssl_keys"):
        why.append("ssl_keys.log missing or empty (v2 keeps the keys)")
    if net.get("capture_gap_minutes"):
        why.append(f"capture gap: minutes with zero client packets {net['capture_gap_minutes']}")
    if not lab.get("n_res_meta"):
        why.append("no res_meta ticks")
    else:
        if lab["tick_presence"] < CHECKS["tick_presence_min"]:
            why.append(f"res_meta ticks in {lab['tick_presence']:.3f} of seconds < {CHECKS['tick_presence_min']}")
        a = lab.get("advancing_share_while_playing")
        if a is not None and a < CHECKS["advancing_share_min"]:
            why.append(f"current_time advancing in {a:.2f} of playing ticks < {CHECKS['advancing_share_min']}")
        if not lab.get("v2_columns"):
            why.append("labels.csv lacks the v2 columns (old extension loaded?)")
        fz, T = lab.get("frozen_playhead_max_s"), lab.get("frozen_playhead_T_s")
        if T is not None and fz is not None and fz > T and abort_reason != "player_frozen":
            why.append(f"player_frozen: playhead frozen {fz:.0f} s in a stall > T {T} s (addendum N)")
        dr, DT = lab.get("dead_player_max_s"), lab.get("dead_player_T_s")
        if DT is not None and dr is not None and dr >= DT and abort_reason != "player_dead":
            why.append(f"player_dead: no media (readyState 0) for {dr:.0f} s after playback >= T {DT} s")
        sa = lab.get("stall_advancing_share")
        if sa is not None and sa > STALL_ADVANCING_SHARE_MAX:
            why.append(f"latched_stall (I10): {sa:.2f} of within-stall ticks with the playhead advancing at "
                       f"{STALL_ADVANCING_RATE} s/s > {STALL_ADVANCING_SHARE_MAX}")
        if lab.get("usable_share") is not None and lab["usable_share"] < CHECKS["usable_share_min"]:
            why.append(f"usable (non-ad play or stall) {lab['usable_share']:.2f} of the session < {CHECKS['usable_share_min']}")
    if net.get("ipv6_media_share"):
        why.append(f"ipv6_media: {net['ipv6_media_share']:.3f} of media bytes on IPv6 flows (old data is IPv4-only)")
    exo = net.get("exogenous_share")
    if exo is not None and exo > max_bg:
        why.append(f"exogenous background (Chrome / OS / unnamed flows) {exo:.3f} of shaper-window downstream > {max_bg}")
    if shots.get("share") is not None and shots["share"] < CHECKS["shots_share_min"]:
        why.append(f"screenshots {shots['n']} of ~{shots['expected']} expected")
    if meta_missing:
        why.append(f"meta.json incomplete: {meta_missing}")
    return ("FAIL" if why else "PASS"), why


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--move-failed", action="store_true")
    ap.add_argument("--max-background-share", type=float, default=CHECKS["max_background_share"])
    a = ap.parse_args()
    sd = a.session_dir.resolve()
    meta, meta_missing = meta_check(sd)
    platform = meta.get("platform") or sd.parent.name
    label_rows = read_labels(sd)
    lab = label_checks(label_rows, platform, meta.get("player_dead_reentries"))
    lab["shot_effects"] = shot_effects(label_rows, sd)
    if platform == "tiktok":
        lab["tt_flv_tokens_per_room"] = tt_flv_tokens(sd, lab.get("tt_room_visits") or [])
    net = traffic_checks(sd, platform)
    # independent of the pcap (a header-only pcap used to return early and report the keys missing; chaos smoke run)
    keys = sd / "ssl_keys.log"
    net["ssl_keys"] = keys.exists() and keys.stat().st_size > 0
    shot_files = list((sd / "shots").glob("*.jpg")) if (sd / "shots").exists() else []
    dur = lab.get("duration_s") or (net.get("minutes") or 0) * 60
    exp_n = int(dur / CHECKS["shot_interval_s"]) if dur else 0
    shot_reasons = collections.Counter()
    if (sd / "shots" / "index.jsonl").exists():
        for l in (sd / "shots" / "index.jsonl").read_text().splitlines():
            try:
                shot_reasons[json.loads(l).get("reason") or "cadence"] += 1
            except Exception:
                pass
    # the cadence shots carry the integrity check; event shots (tier change / ad start, t37) are extra
    n_cad = shot_reasons.get("cadence", len(shot_files)) if shot_reasons else len(shot_files)
    shots = {"n": n_cad, "expected": exp_n, "share": (n_cad / exp_n) if exp_n else None, "by_reason": dict(shot_reasons)}
    # G3 ground truth (never features): player_stats.jsonl records; twitch records carrying the Video Stats overlay
    ps_n, ps_tw = 0, 0
    if (sd / "player_stats.jsonl").exists():
        for l in (sd / "player_stats.jsonl").read_text().splitlines():
            ps_n += 1
            ps_tw += '"twitch_video_stats"' in l
    player_stats = {"records": ps_n, "twitch_video_stats_records": ps_tw}
    acts = collections.Counter()
    if (sd / "ad_actions.jsonl").exists():
        for l in (sd / "ad_actions.jsonl").read_text().splitlines():
            if l.strip():
                a_ = json.loads(l)
                acts[a_["action"].split(" [")[0] + (f":{a_['reason']}" if a_.get("reason") else "")] += 1
    pin = meta.get("player_pin") or {}
    try:
        import motion
        motion_s = motion.read_summary(sd)
    except Exception as e:
        motion_s = {"status": "error", "why": repr(e)}
    v, why = verdict(lab, net, shots, meta_missing, a.max_background_share, meta.get("abort_reason"))
    # levi 2026-10-17: entries (from meta.json, else computed here for older sessions) and one session class by the
    # fixed precedence; only `normal` counts toward quotas
    ents = meta.get("entries")
    if ents is None:
        ents = E.compute_entries(label_rows, meta, E.read_net_labels(sd))
    s_class = E.session_class(platform, v, meta.get("abort_reason"), ents, lab.get("dead_player_max_s"),
                              lab.get("dead_player_T_s"), lab.get("frozen_playhead_max_s"), lab.get("frozen_playhead_T_s"),
                              recovered=bool(lab.get("recovered_dead")))
    slow_start = E.slow_start_shaped(ents)
    sreg = SR.session_regime(sd, platform, meta)   # reported, never part of the verdict or the class
    out = {"session": str(sd), "platform": platform, "verdict": v, "session_class": s_class, "entries": ents,
           "slow_start_shaped": slow_start,
           "reasons": why, "thresholds": CHECKS,
           "max_background_share": a.max_background_share, "labels": lab, "traffic": net, "shots": shots,
           "ad_actions": dict(acts), "player_pin": pin, "meta_missing": meta_missing, "player_stats": player_stats, "motion": motion_s,
           "matrix": (meta.get("shaper") or {}).get("matrix_id"), "low_state_cap": (meta.get("shaper") or {}).get("low_state_cap"),
           "stream_regime": sreg["regime"], "suspicious": sreg["suspicious"]}
    (sd / "check.json").write_text(json.dumps(out, indent=1, default=str))
    md = [f"# check: {platform}/{sd.name} — **{v}** ({s_class})", ""] + [f"- {r}" for r in why] + \
        [f"- SUSPICIOUS (reported; verdict unchanged): {x}" for x in sreg["suspicious"]] + ["",
          "| item | value |", "|---|---|",
          f"| session class (levi's precedence; only normal counts) | {s_class} |",
          f"| slow start under shaping (stream not sustained in 60 s, first shaped state <= {E.SLOW_START_MAX_STATE}; covariate, class stays) | {slow_start} |",
          f"| entries: kind / state at entry / first shaped state / sustained >= 5 s within 60 s | {[(x.get('kind'), (x.get('shaper_at_entry') or {}).get('state'), (x.get('first_shaped_after') or {}).get('state'), x.get('sustained_60s')) for x in ents]} |",
          f"| longest dead-player run after playback (readyState 0 / no media), s / T | {lab.get('dead_player_max_s')} / {lab.get('dead_player_T_s')} |",
          f"| recovered dead run (levi (a): the run holding an outcome-continue re-entry, not the verdict / class; covariate) s / window / class rule | {(lab.get('recovered_dead') or {}).get('recovered_dead_s')} / {(lab.get('recovered_dead') or {}).get('window')} / {E.RECOVERED_DEAD_CLASS} |",
          f"| duration (min) | {dur / 60:.1f} |" if dur else "| duration | — |",
          f"| res_meta tick presence (share of seconds) | {lab.get('tick_presence')} |",
          f"| current_time advancing (playing, non-ad) | {lab.get('advancing_share_while_playing')} |",
          f"| usable share (non-ad play or stall) | {lab.get('usable_share')} |",
          f"| ad ticks per signal | {lab.get('ad_ticks_per_signal')} |",
          f"| ad spans (span-type set) / seconds / per signal | {lab.get('ad_spans_span_type_set')} / {lab.get('ad_span_seconds')} / {lab.get('ad_spans_per_signal')} |",
          f"| Addendum D: swap-only ad spans (must be 0) | {lab.get('swap_only_ad_spans')} |",
          f"| Addendum D: tier changes with src_changed within ± 2 s | {lab.get('tier_changes_with_swap_within_2s_share')} (of {lab.get('tier_changes')}) |",
          f"| stall share | {lab.get('stall_share')} |",
          f"| longest frozen-playhead stall run, s / T (FAIL above; addendum N) | {lab.get('frozen_playhead_max_s')} / {lab.get('frozen_playhead_T_s')} |",
          f"| paused while labelled playing (share of playing ticks; suspicious > {PAUSED_WHILE_PLAYING_MAX}) | {lab.get('paused_while_playing_share')} |",
          f"| I10: stall ticks with the playhead advancing (FAIL > {STALL_ADVANCING_SHARE_MAX}; >= {I10_MIN_STALL_TICKS} stall ticks) | {lab.get('stall_advancing_share')} |",
          f"| tier mix (quality time, ads masked) | {lab.get('tier_mix_quality_time')} |",
          f"| yt 144 / 240 / 360p shares | {lab.get('yt_low_tier_shares')} |",
          f"| tier changes / min | {lab.get('tier_changes_per_min')} |",
          f"| exogenous background share, shaper window (enforced, addendum I) | {net.get('exogenous_share')} |",
          f"| all background share, shaper window (episode max) / whole capture / by http_v2 | {net.get('background_share')} ({net.get('background_share_episode_max')}) / {net.get('background_share_whole_capture')} / {net.get('background_share_http_v2')} |",
          f"| shares by category (shaper window) | {net.get('category_shares')} |",
          f"| downstream before the first episode (MB; page load) | {net.get('pre_shaper_downstream_mb')} |",
          f"| 60 s windows with exogenous > 0.25 x their downstream (reported; addenda G / I) | {net.get('background_heavy_windows')} |",
          f"| shaper binds (share of episodes; reported QA only, addendum G) | {net.get('shaper_bind_share')} |",
          f"| Addendum C (QA): dwell per state, episodes (seconds / 60) | {net.get('dwell_episodes_per_state')} |",
          f"| longest low-state run, episodes / low-state cap | {net.get('low_run_max_episodes')} / {out['low_state_cap']} |",
          f"| IPv6 share of media bytes (FAIL > 0) / of all downstream; IPv6 sysctl at start | {net.get('ipv6_media_share')} / {net.get('ipv6_downstream_share')}; {meta.get('ipv6_sysctl')} |",
          f"| matrix / low-state cap | {out['matrix']} / {out['low_state_cap']} |",
          f"| login state / room rotation (addendum M) | {meta.get('login_state')} / {(meta.get('room_rotation') or {}).get('active')} ({len((meta.get('room_rotation') or {}).get('rooms') or [])} rooms) |",
          f"| channel (handle / type) | {meta.get('channel_handle')} / {meta.get('channel_type')} |",
          f"| login wall: ticks flagged / text-only ticks / abort | {lab.get('login_wall_ticks')} / {lab.get('login_wall_text_ticks')} / {meta.get('abort_reason')} |",
          f"| tt logged-out quality countdown ticks (FAIL if > 0) | {lab.get('quality_countdown_ticks')} |",
          f"| screenshot effect (ticks within 1 s of a cadence shot vs elsewhere): dropped frames per tick / client size off mode | {(lab.get('shot_effects') or {}).get('dropped_per_tick_near')} vs {(lab.get('shot_effects') or {}).get('dropped_per_tick_elsewhere')} / {(lab.get('shot_effects') or {}).get('client_size_off_mode_near')} vs {(lab.get('shot_effects') or {}).get('client_size_off_mode_elsewhere')} |",
          f"| motion (addendum O cross-check; 10 s shots, play-only pairs): valid pairs / session / median / p90 / near-zero share / scene cuts | {(motion_s or {}).get('valid_pairs')} / {(motion_s or {}).get('session_motion')} / {(motion_s or {}).get('median')} / {(motion_s or {}).get('p90')} / {(motion_s or {}).get('share_near_zero')} / {(motion_s or {}).get('scene_cuts')} ({(motion_s or {}).get('status')}) |",
          f"| twitch category / type (addendum O) | {meta.get('tw_category')} / {meta.get('tw_category_type')} |",
          f"| twitch pick: candidates / recent channels excluded / pick | {len((meta.get('tw_pick') or {}).get('candidates') or [])} / {len((meta.get('tw_pick') or {}).get('excluded_recent') or [])} / {(meta.get('tw_pick') or {}).get('pick')} |",
          f"| tt room choice: candidates / used in recent sessions / tries / choice | {len((meta.get('tt_room_choice') or {}).get('candidates') or [])} / {sum(1 for c in (meta.get('tt_room_choice') or {}).get('candidates') or [] if c.get('recent'))} / {[(t.get('room'), t.get('ok'), (t.get('detail') or {}).get('dead')) for t in (meta.get('tt_room_choice') or {}).get('tries') or []]} / {(meta.get('tt_room_choice') or {}).get('choice')} |",
          f"| tt room ended mid-session (reason / at) | {(meta.get('room_ended') or {}).get('reason')} / {(meta.get('room_ended') or {}).get('at')} |",
          f"| stream regime vs expected (stream_regime.py: played codec / ladder / player px / reachable top / played top) | {(sreg['regime'] or {}).get('played_codec')} / {(sreg['regime'] or {}).get('ladder')} / {(sreg['regime'] or {}).get('player_h_device_px')} / {(sreg['regime'] or {}).get('reachable_top')} / {(sreg['regime'] or {}).get('played_top')}; flags {SR.flag_keys(sreg['suspicious'])} |",
          f"| tt stream: codec (inferred from the suffix) / requested / ladder per codec / quality button | {(meta.get('tt_stream') or {}).get('codec')} ({(meta.get('tt_stream') or {}).get('codec_inferred_from_suffix')}) / {(meta.get('tt_stream') or {}).get('requested')} / { {k: len(v) for k, v in ((meta.get('tt_stream') or {}).get('ladder') or {}).items()} } / {(meta.get('tt_stream') or {}).get('show_quality_button')} |",
          f"| tt player_dead re-entries (one allowed; outcome continue / room_ended / player_dead) | {[(r.get('outcome'), (r.get('dead_window') or {}).get('stream')) for r in meta.get('player_dead_reentries') or []]} |",
          f"| tt player_dead: dead run s / shaped downstream kbit/s / shaper kbit / share / stream (alive = the player stopped consuming a live stream) | {(meta.get('player_dead') or {}).get('dead_s')} / {((meta.get('player_dead') or {}).get('dead_window') or {}).get('shaped_rx_kbps')} / {((meta.get('player_dead') or {}).get('dead_window') or {}).get('shaper_kbit')} / {((meta.get('player_dead') or {}).get('dead_window') or {}).get('share_of_shaper')} / {((meta.get('player_dead') or {}).get('dead_window') or {}).get('stream')} |",
          f"| tt player discontinuities (source drops that came back): count / total s / after a stall | {len(meta.get('room_discontinuities') or [])} / {round(sum(d.get('duration_s', 0) for d in meta.get('room_discontinuities') or []), 1)} / {sum(1 for d in meta.get('room_discontinuities') or [] if d.get('stall_recent'))} |",
          f"| tt rooms (ticks per room) / room changes / explore-page ticks | {lab.get('tt_room_ticks')} / {lab.get('tt_room_changes')} / {lab.get('tt_explore_ticks')} |",
          f"| tt FLV rendition tokens per room (http_v2) | {lab.get('tt_flv_tokens_per_room')} |",
          f"| tt resets 55-75 s into a visit with a lower rendition after (reported) | {lab.get('tt_cap_resets_55_75s')} |",
          f"| profile template (path / sha) | {(meta.get('profile_template') or {}).get('path')} / {(meta.get('profile_template') or {}).get('sha256', '')[:12]} |",
          f"| capture gap minutes | {net.get('capture_gap_minutes')} |",
          f"| ssl keys kept | {net.get('ssl_keys')} |",
          f"| screenshots: cadence of ~expected / by reason | {shots['n']} of ~{shots['expected']} / {shots.get('by_reason')} |",
          f"| player_stats.jsonl records (G3; twitch: with Video Stats) | {player_stats['records']} ({player_stats['twitch_video_stats_records']}) |",
          f"| ad actions | {dict(acts)} |",
          f"| geometry drawn / realised player / window / ok (clamped to the screen) | {meta.get('geometry_target') or pin.get('geometry_target')} / {pin.get('final_player')} / {pin.get('final_window')} / {pin.get('ok')} ({pin.get('clamped_to') if pin.get('clamped') else 'no'}) |",
          f"| meta.json missing | {meta_missing} |"]
    if net.get("background_hosts_top"):
        md += ["", "Background hosts, shaper window (top by bytes; exogenous = chrome_service / os_service / unknown):", "",
               "| host | category | share of downstream | MB |", "|---|---|---|---|"]
        md += [f"| {h['host']} | {h['category']} | {h['share']} | {h['mb']} |" for h in net["background_hosts_top"]]
    if net.get("unknown_hosts_top"):
        md += ["", f"Unnamed flows above {CHECKS['unknown_list_share']} of downstream: top servers (check for a missed video CDN):",
               "", "| server:port/proto | share of downstream | MB |", "|---|---|---|"]
        md += [f"| {h['server']} | {h['share']} | {h['mb']} |" for h in net["unknown_hosts_top"]]
    (sd / "check.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    if v == "FAIL" and a.move_failed:
        dest_root = sd.parent.parent / "_failed" / sd.parent.name
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / sd.name
        k = 1
        while dest.exists():
            dest = dest_root / f"{sd.name}_{k}"
            k += 1
        shutil.move(str(sd), str(dest))
        print(f"FAIL → moved to {dest}")
    return 0 if v == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
