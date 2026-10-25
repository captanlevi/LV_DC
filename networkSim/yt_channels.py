"""yt_channels.py: the collection v2 YouTube channel list and its rotation (LVQoE prereg collection_v2 §1 / G5, levi R2).

Criteria: 24/7 live, public, not age-gated or member-only. Content types: news, sport, gaming, music, nature_cams, each
with 2 primary channels from different owners. The content type is assigned here, before capture, and recorded in
meta.json; it is never inferred from the data. Spares substitute for a primary of the same type that is not live.

Verified 2026-10-16 on the laptop (Chrome 155; scripts in the LVQoE t35 report): every entry's /live page showed the
live badge, playback advanced, and no age or members gate appeared.

Rotation: no channel gets a 3rd session until every primary has 2 (generally: always the least-collected primary,
ties broken by list order), and never the previous yt session's channel while another is possible (no immediate repeat,
supervisor 2026-10-19; each pick is logged to _summaries/yt_channel_picks.jsonl). Sessions are counted from every youtube/<session>/meta.json under data/ (channel_handle),
excluding data/_failed (a failed session does not count).

List maintenance (prereg Addendum P, levi 2026-10-17): a channel leaves outright when, in a rolling LEAVE_WINDOW_S, it has
>= LEAVE_NOT_LIVE not-live observations >= LEAVE_MIN_GAP_S apart (start failures, or pre-checks that found it off air)
and 0 accepted sessions (spares too). A type left with fewer than 2 owners gets its spare promoted through the live check. The collector applies it at the pick and writes
the leave to data/_summaries/yt_list_leaves.jsonl, which keeps it out after the window; the leave is then committed
here (LIST_CHANGES, REMOVED_CHANNELS) and in Addendum P. A spare is promoted only after its live check passes. Every
session's meta.json carries the list record (list_record: version and every change, committed or on disk).

Provisional channels (levi 2026-10-26 03:29, gate list: a live check proves liveness, not motion): a spare, and a spare
promoted with "provisional": True, is provisional until its first collected session reads static share
<= STATIC_SHARE_MAX (brad's post-copy QA read), committed here as a LIST_CHANGES "static_read" entry. While provisional
the picker holds it after one accepted session until that read is committed (no second session before the read), and
pauses it on a recorded fail (it goes to rushi under the static-channel rule). static_holds() names both.

Not-live skips (shagun 2026-10-17: @Sypherpkvods, never collected, so always least-collected, failed not-live on yt_7,
yt_12 and yt_30): read from the not-live failures archived in data/_failed, so they survive a batch restart. One in the
last NOT_LIVE_RETRY_S skips the channel that long; NOT_LIVE_STRIKES within NOT_LIVE_OUT_S put it out for NOT_LIVE_OUT_S
after the latest.
"""
from __future__ import annotations

import json
import math
import random
import re
import time
import urllib.request
from pathlib import Path

NOT_LIVE_MARK = "Failed to navigate to a live"   # browser.py's start failure, kept in meta.json abort_reason
NOT_LIVE_RETRY_S = 6 * 3600
NOT_LIVE_STRIKES = 2
NOT_LIVE_OUT_S = 24 * 3600

YT_CHANNELS: list[dict] = [
    {"handle": "@AlJazeeraEnglish", "type": "news", "owner": "Al Jazeera Media Network", "role": "primary"},
    {"handle": "@dwnews", "type": "news", "owner": "Deutsche Welle", "role": "primary"},
    {"handle": "@MonsterJam", "type": "sport", "owner": "Feld Entertainment (Monster Jam)", "role": "primary"},
    {"handle": "@RetroWorldVideos", "type": "gaming", "owner": "RetroWorld", "role": "primary"},
    {"handle": "@LofiGirl", "type": "music", "owner": "Lofi Girl", "role": "primary"},
    {"handle": "@everybodychillradio", "type": "music", "owner": "Everybody Chill Radio", "role": "primary"},
    {"handle": "@SBC-TV1", "type": "nature_cams", "owner": "SBC-TV (Maasai Mara Live Safari)", "role": "primary"},
    {"handle": "@ScottishWildlifeGarden", "type": "nature_cams", "owner": "Scottish Wildlife Garden", "role": "primary"},
    {"handle": "@channelnewsasia", "type": "news", "owner": "Mediacorp (CNA)", "role": "spare"},
    {"handle": "@FuboSports", "type": "sport", "owner": "Fubo Sports Network", "role": "spare"},
    {"handle": "@BeanBoltTV", "type": "gaming", "owner": "BeanBoltTV", "role": "primary"},   # promoted 2026-10-18
    {"handle": "@SkyofFairyTales", "type": "music", "owner": "Sky of Fairy Tales", "role": "spare"},
    {"handle": "@dakotaspond", "type": "nature_cams", "owner": "Dakota's Pond", "role": "spare"},
    {"handle": "@TipTank247", "type": "nature_cams", "owner": "TipTank247", "role": "spare"},
]

# archive, never delete: channels taken off the list, as they were listed
REMOVED_CHANNELS: list[dict] = [
    {"handle": "@Sypherpkvods", "type": "gaming", "owner": "SypherPK", "role": "primary"},
    {"handle": "@FUELTVMoto", "type": "sport", "owner": "FUEL TV", "role": "primary"},
]
# every list change after the 2026-10-16 verification (version = the number of changes)
LIST_CHANGES: list[dict] = [
    {"date": "2026-10-17", "change": "removed", "handle": "@Sypherpkvods", "type": "gaming", "role": "primary",
     "rule": "prereg Addendum P (levi)",
     "evidence": "0 accepted sessions, 3 not-live failures on chaos (yt_7, yt_12, yt_30); /live redirects to the channel page"},
    {"date": "2026-10-18", "change": "promoted", "handle": "@BeanBoltTV", "type": "gaming", "role": "primary",
     "rule": "prereg Addendum P (levi): a spare is promoted after its live check (>= 3 live checks >= 6 h apart spanning "
             ">= 24 h, no not-live inside the span)",
     "evidence": "channel_checks/yt_live_checks.jsonl: live at ts 1792231224.3 (2026-10-17T10:00Z), 1792255231.3 "
                 "(2026-10-17T16:40Z), 1792298394.1 (2026-10-18T04:39Z), 1792320384.0 (2026-10-18T10:46Z); span 24.8 h; "
                 "top 1440p; a different owner from the gaming primary @RetroWorldVideos"},
    {"date": "2026-10-26", "change": "removed", "handle": "@FUELTVMoto", "type": "sport", "role": "primary",
     "rule": "static source (rushi 01:1x, levi 22:56: a channel with static share > 0.5 in >= 2 sessions goes to rushi before "
             "the next window; rushi: drop it); the sport type's spare is promoted only after its Addendum P live check",
     "evidence": "a source-side still ('WSX Vancouver', a crowd image) on 2 of 2 dry sessions with the player presenting "
                 "normally: W-B yt_100001 (decoded mad < 0.5 on 0.996 of frames, peter 2fc266a9) and WC2 yt_100001 "
                 "(shots byte-identical across ~25 min, shagun 22:56)"},
]
LEAVE_WINDOW_S = 48 * 3600
LEAVE_NOT_LIVE = 3
LEAVE_MIN_GAP_S = 6 * 3600
LEAVES_FILE = Path("_summaries") / "yt_list_leaves.jsonl"
PRECHECK_SKIPS_FILE = Path("_summaries") / "yt_precheck_skips.jsonl"
PICKS_FILE = Path("_summaries") / "yt_channel_picks.jsonl"


LONG_NOT_LIVE_S = 6 * 3600     # levi (t56, BeanBoltTV): a primary pre-checked not live for >= 6 h leaves the tie
REENTRY_PROBES_PER_PICK = 2


def long_not_live(data_dirs: list[Path], now: float, span_s: float = LONG_NOT_LIVE_S) -> dict[str, dict]:
    """Primaries unavailable for the least-collected tie (levi): every pre-check since the channel was last seen live (a
    probe that read live, or an accepted session's start) read not live, at least two of them, the first >= span_s
    ago. {handle: {first, last, n}}; it re-enters when a probe reads it live (a "live" record in the pre-check log)."""
    recs, starts = [], {}
    for d in data_dirs:
        f = Path(d) / PRECHECK_SKIPS_FILE
        if f.exists():
            for line in f.read_text().splitlines():
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
        for h, ts in accepted_starts(Path(d)).items():
            starts[h] = max([starts.get(h, 0.0)] + ts)
    out = {}
    for c in (c for c in YT_CHANNELS if c["role"] == "primary"):
        h = c["handle"]
        mine = sorted((r for r in recs if r.get("handle") == h and isinstance(r.get("ts"), (int, float))),
                      key=lambda r: r["ts"])
        seen = max([starts.get(h, 0.0)] + [r["ts"] for r in mine if r.get("state") == "live"])
        nl = [r["ts"] for r in mine if r["ts"] > seen and r.get("state") == "not_live"]
        if len(nl) >= 2 and now - nl[0] >= span_s:
            out[h] = {"first": nl[0], "last": nl[-1], "n": len(nl)}
    return out


def pick_record(data_dir: Path, channel: dict, select=None) -> dict:
    """What the rotation saw when it picked (logged per pick): the channel, the previous session's, the counts."""
    counts = collected_counts(data_dir, select)
    return {"ts": time.time(), "handle": channel.get("handle"), "role": channel.get("role"), "type": channel.get("type"),
            "last": last_channel(data_dir, select), "no_immediate_repeat": NO_IMMEDIATE_REPEAT, "rule_state": dict(LAST_PICK),
            "counts": {c["handle"]: counts.get(c["handle"], 0) for c in YT_CHANNELS if c["role"] == "primary"}}


def url_of(handle: str) -> str:
    return f"https://www.youtube.com/{handle}/live"


def by_handle(handle: str) -> dict | None:
    return next((c for c in YT_CHANNELS if c["handle"] == handle), None)


def collected_counts(data_dir: Path, select=None) -> dict[str, int]:
    """Accepted yt sessions per channel handle: every <...>/youtube/<session>/meta.json under data_dir (so both
    data/youtube/ and data/<collector>/youtube/ count), except anything under _failed/; select(meta) -> bool: only
    the sessions it keeps (the trio: its sessions of record, levi)."""
    counts = {c["handle"]: 0 for c in YT_CHANNELS}
    metas = (m for m in data_dir.rglob("meta.json") if m.parent.parent.name == "youtube" and "_failed" not in m.parts
             and not any(x.startswith("_trio") for x in m.relative_to(data_dir).parts))                 # trio sessions have their own root and never count here
    for meta in sorted(metas):
        try:
            m = json.loads(meta.read_text())
        except Exception:
            continue
        if select is not None and not select(m):
            continue
        h = m.get("channel_handle")
        if h in counts:
            counts[h] += 1
    return counts


def not_live_failures(data_dir: Path) -> dict[str, list[float]]:
    """Start times of the not-live failures per channel handle: data/_failed/.../youtube/<session>/meta.json."""
    out: dict[str, list[float]] = {}
    metas = (m for m in data_dir.rglob("meta.json") if m.parent.parent.name == "youtube" and "_failed" in m.parts
             and not any(x.startswith("_trio") for x in m.relative_to(data_dir).parts))
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


def accepted_starts(data_dir: Path) -> dict[str, list[float]]:
    """Start times of the accepted (not _failed) sessions per channel handle."""
    out: dict[str, list[float]] = {}
    metas = (m for m in data_dir.rglob("meta.json") if m.parent.parent.name == "youtube" and "_failed" not in m.parts
             and not any(x.startswith("_trio") for x in m.relative_to(data_dir).parts))
    for meta in metas:
        try:
            m = json.loads(meta.read_text())
        except Exception:
            continue
        if m.get("channel_handle") and m.get("started_at"):
            out.setdefault(m["channel_handle"], []).append(float(m["started_at"]))
    return out


def disk_leaves(data_dir: Path) -> list[dict]:
    """Leaves the collector applied (data/_summaries/yt_list_leaves.jsonl), oldest first."""
    p = data_dir / LEAVES_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def not_live_observations(data_dir: Path) -> dict[str, list[float]]:
    """Times each channel was seen off air: not-live start failures (data/_failed) and pre-checks that found it not live
    (_summaries/yt_precheck_skips.jsonl, state not_live)."""
    out = {h: list(ts) for h, ts in not_live_failures(data_dir).items()}
    p = data_dir / PRECHECK_SKIPS_FILE
    if p.exists():
        for l in p.read_text().splitlines():
            try:
                r = json.loads(l)
            except Exception:
                continue
            if r.get("state") == "not_live" and r.get("handle") and r.get("ts"):
                out.setdefault(r["handle"], []).append(float(r["ts"]))
    return out


def _spaced(ts: list[float], gap: float) -> list[float]:
    """The times taken greedily, oldest first, each >= gap after the previous one taken."""
    out: list[float] = []
    for t in sorted(ts):
        if not out or t - out[-1] >= gap:
            out.append(t)
    return out


def apply_leave_rule(data_dir: Path, now: float) -> list[dict]:
    """Addendum P: listed channels with >= LEAVE_NOT_LIVE not-live observations >= LEAVE_MIN_GAP_S apart and 0 accepted
    sessions in the last LEAVE_WINDOW_S leave; each new leave is appended to the leaves file. Returns the new leaves."""
    gone = {r["handle"] for r in disk_leaves(data_dir)}
    obs, ok = not_live_observations(data_dir), accepted_starts(data_dir)
    new = []
    for c in YT_CHANNELS:
        h = c["handle"]
        nl = _spaced([t for t in obs.get(h, []) if 0 <= now - t < LEAVE_WINDOW_S], LEAVE_MIN_GAP_S)
        if h in gone or len(nl) < LEAVE_NOT_LIVE or any(0 <= now - t < LEAVE_WINDOW_S for t in ok.get(h, [])):
            continue
        new.append({"ts": now, "change": "removed", "handle": h, "type": c["type"], "role": c["role"],
                    "rule": "prereg Addendum P (collector)",
                    "evidence": f"{len(nl)} not-live observations >= {LEAVE_MIN_GAP_S // 3600} h apart and 0 accepted "
                                f"sessions in {LEAVE_WINDOW_S // 3600} h",
                    "not_live_at": nl})
    if new:
        (data_dir / LEAVES_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(data_dir / LEAVES_FILE, "a") as fh:
            for r in new:
                fh.write(json.dumps(r) + "\n")
    return new


def spares_needing_promotion(leaves: list[dict]) -> list[str]:
    """Spares of each content type left with fewer than 2 primary owners (committed list minus the collector's leaves):
    these get the promotion live check (Addendum P)."""
    gone = {r["handle"] for r in leaves}
    out = []
    for t in dict.fromkeys(c["type"] for c in YT_CHANNELS):
        owners = {c["owner"] for c in YT_CHANNELS if c["type"] == t and c["role"] == "primary" and c["handle"] not in gone}
        if len(owners) < 2:
            out += [c["handle"] for c in YT_CHANNELS if c["type"] == t and c["role"] == "spare" and c["handle"] not in gone
                    and c["owner"] not in owners]
    return out



STATIC_SHARE_MAX = 0.5          # levi 03:29: a provisional channel's first collected session at <= this confirms it


def static_reads(handle: str) -> list[dict]:
    """The committed static reads of `handle` (LIST_CHANGES "static_read"), in list order."""
    return [c for c in LIST_CHANGES if c["change"] == "static_read" and c["handle"] == handle]


STATIC_READ_PROVENANCE = ("reader", "sha", "file", "metric")   # brad 03:42: a committed read is citable


def provisional(handle: str) -> bool:
    """levi 03:29: a spare, or a spare promoted with "provisional": True, until a passing static read is committed. Any
    pass ends it for good (brad 03:42): a later static drift of a confirmed channel is the reader's >= 2-session
    static-channel rule (to rushi), not this hold."""
    c = by_handle(handle)
    if c is None or not (c["role"] == "spare" or c.get("provisional")):
        return False
    return not any(r["result"] == "pass" for r in static_reads(handle))


def static_holds(data_dir: Path) -> dict[str, str]:
    """{handle: why} for the provisional channels the picker holds: a recorded static fail (paused, to rushi), or an
    accepted session under data_dir with no static read committed yet (one session until its read). data_dir is the
    picker's own root: the trio's is <data>/_trio itself (orchestrate._pick: out = root), so its youtube/<session>
    dirs count; any accepted session counts, dry or of record (levi 03:29: "its first collected session")."""
    started = accepted_starts(data_dir)
    out = {}
    for c in YT_CHANNELS:
        h = c["handle"]
        if not provisional(h):
            continue
        reads = static_reads(h)
        if reads and reads[-1]["result"] == "fail":
            out[h] = (f"provisional, static share {reads[-1]['static_share']} > {STATIC_SHARE_MAX} on "
                      f"{reads[-1]['session']}: paused, to rushi (static-channel rule)")
        elif not reads and started.get(h):
            out[h] = f"provisional, {len(started[h])} accepted session(s) and no static read committed: held until its read"
    return out

def list_record(data_dir: Path, handle: str | None) -> dict:
    """meta.json's channel list record: the version, the channel's role, every committed change and every leave the
    collector applied on disk."""
    c = by_handle(handle) if handle else None
    leaves = disk_leaves(data_dir)
    return {"version": len({(r["handle"], r["change"]) for r in LIST_CHANGES + leaves}), "role": c["role"] if c else None,
            "changes": LIST_CHANGES, "collector_leaves": leaves}


def last_channel(data_dir: Path, select=None) -> str | None:
    """The channel of the latest single-mode yt session that played (accepted or _failed, by meta.json started_at); a
    not-live start failure is no session of the channel (the not-live skips handle it)."""
    best = None
    for meta in data_dir.rglob("meta.json"):
        if meta.parent.parent.name != "youtube" or any(x.startswith("_trio") for x in meta.relative_to(data_dir).parts):
            continue
        try:
            m = json.loads(meta.read_text())
        except Exception:
            continue
        if select is not None and not select(m):
            continue
        t, h = m.get("started_at"), m.get("channel_handle")
        if NOT_LIVE_MARK in (m.get("abort_reason") or ""):
            continue
        if h and isinstance(t, (int, float)) and (best is None or t > best[0]):
            best = (t, h)
    return best[1] if best else None


NO_IMMEDIATE_REPEAT = True     # supervisor 2026-10-19 (06:50 audit: a newly promoted primary was drawn back to back)
# levi 2026-10-20 (peter 00221: at every era restart the per-era counts tie at 0 and list order drew AlJazeera then dwnews,
# on the class order's low-tier targets): ties go to a seeded shuffle, never list order; the seed is CHANNEL_TIE_SEED +
# the root's yt sessions (any outcome), recorded with the pick (meta.json channel_pick). 3845ee7 tied the least-collected;
# the full rule below (DIVERSITY_RULE) ties the least-recently-drawn.
CHANNEL_TIE_SEED = 0


def draw_seed(data_dir: Path) -> int:
    """CHANNEL_TIE_SEED + every yt session dir under data_dir (accepted and _failed): one seed per draw, reproducible."""
    root = Path(data_dir)
    n = sum(1 for d in list((root / "youtube").glob("yt_*")) + list((root / "_failed" / "youtube").glob("yt_*"))
            if d.is_dir())
    return CHANNEL_TIE_SEED + n


# levi's ruling 00133, the full rule (the next count boundary; channel identity only, never QoE, measured yield or labels):
#   1. least-recently-drawn first, over every yt draw under the root (accepted and _failed, any era, pilots included:
#      identity is content, not era data), never-drawn first, ties by the seeded shuffle (CHANNEL_TIE_SEED + draws);
#   2. a distinct-channel floor F (CHANNEL_FLOOR, else the listed primaries: yt 10): while fewer than F channels have a
#      session of record (select), a channel that already has one is passed over while an eligible 0-count one exists
#      (a never-live channel cannot block it);
#   3. a per-channel cap: a channel at >= max(2, ceil(CHANNEL_CAP_MULT / F x n_record)) sessions of record is passed over
#      while another eligible channel is below it.
# Eligible: a primary not excluded (not live, sibling, set aside, left the list), or a spare of a type whose primaries
# are all excluded. Every stage is recorded with the pick (LAST_PICK -> the pick record -> meta.json channel_pick).
CHANNEL_FLOOR: int | None = None
CHANNEL_CAP_MULT = 2
DIVERSITY_RULE = ("levi 00133: least-recently-drawn across every draw (any era, pilots), seeded tie; floor F = the "
                  "listed primaries; cap max(2, ceil(2 / F x n_record)); channel identity only; counts over the counted "
                  "sessions of every era (restart prereg §0)")
LAST_PICK: dict = {}


def last_draws(data_dir: Path) -> dict[str, float]:
    """The latest started_at per channel handle over every yt session dir under data_dir, accepted and _failed, any era,
    pilots included (the trio root's own; under the main root, trio sessions excluded as collected_counts does)."""
    out: dict[str, float] = {}
    root = Path(data_dir)
    for meta in root.rglob("meta.json"):
        if meta.parent.parent.name != "youtube" or any(x.startswith("_trio") for x in meta.relative_to(root).parts):
            continue
        try:
            m = json.loads(meta.read_text())
        except Exception:
            continue
        h, t = m.get("channel_handle"), m.get("started_at")
        if h and isinstance(t, (int, float)) and t > out.get(h, float("-inf")):
            out[h] = float(t)
    return out


def next_channel(data_dir: Path, exclude: set[str] | None = None, no_repeat: bool | None = None, select=None) -> dict:
    """The next channel by DIVERSITY_RULE (the comment above). The previous draw is least recent by construction, so
    no immediate repeat while another channel is eligible (no_repeat kept for the callers; the rule implies it)."""
    holds = static_holds(data_dir)                                   # levi 03:29: provisional channels
    exclude = set(exclude or ()) | {r["handle"] for r in disk_leaves(data_dir)} | set(holds)
    counts = collected_counts(data_dir, select)
    prim = [c for c in YT_CHANNELS if c["role"] == "primary"]
    seed = draw_seed(data_dir)
    rnd = random.Random(seed)
    key = {c["handle"]: rnd.random() for c in YT_CHANNELS}          # the seeded shuffle (list order fixes the draws)
    elig = [p for p in prim if p["handle"] not in exclude]
    types_out = {p["type"] for p in prim} - {p["type"] for p in elig}
    elig += [c for c in YT_CHANNELS if c["role"] == "spare" and c["type"] in types_out and c["handle"] not in exclude]
    if not elig:
        raise RuntimeError("no live channel left in the list")
    F = CHANNEL_FLOOR or len(prim)
    n_rec = sum(counts.values())
    cap = max(2, math.ceil(CHANNEL_CAP_MULT / F * n_rec))
    distinct = sum(1 for n in counts.values() if n > 0)
    cands, stages = list(elig), []
    zero = [c for c in cands if counts[c["handle"]] == 0]
    if distinct < F and zero and len(zero) < len(cands):
        cands, stages = zero, stages + ["floor"]
    below = [c for c in cands if counts[c["handle"]] < cap]
    if below and len(below) < len(cands):
        cands, stages = below, stages + ["cap"]
    last = last_draws(data_dir)
    cands.sort(key=lambda c: (last.get(c["handle"], float("-inf")), key[c["handle"]]))
    first = last.get(cands[0]["handle"], float("-inf"))
    LAST_PICK.clear()
    LAST_PICK.update(rule=DIVERSITY_RULE, seed=seed, floor=F, distinct_record=distinct, n_record=n_rec, cap=cap,
                     stages=stages or ["lru"], candidates=[c["handle"] for c in cands],
                     tied=sorted(c["handle"] for c in cands if last.get(c["handle"], float("-inf")) == first),
                     counts={c["handle"]: counts[c["handle"]] for c in elig},
                     last_draw={c["handle"]: last.get(c["handle"]) for c in elig}, drawn=cands[0]["handle"],
                     static_holds=holds)
    return cands[0]


# ---------------------------------------------------------------------------
# Pre-session liveness probe (shagun 2026-10-17, rushi: "smooth collection"; peter's notes). orchestrate runs it between
# sessions, outside capture and shaping, so a channel that is off air costs a page fetch instead of a FAIL session.
# Fail open: only positive not-live evidence skips a channel; an HTTP error, a consent redirect or a page without the
# markers runs the session as before (a probe outage must not empty the rotation). Pre-check skips are logged to
# _summaries/yt_precheck_skips.jsonl and do not count as not-live strikes (re-probing is free); strikes keep counting
# only the start failures the probe missed.
# ---------------------------------------------------------------------------

PROBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def classify_live_page(final_url: str, html: str) -> str:
    """'live' / 'not_live' / 'unknown' for a fetched /@handle/live page. Live: "isLiveNow":true. Not live: (a)
    "isLiveNow":false, or (b) no isLiveNow and the canonical link is the channel page (/channel/UC..., the off-air
    redirect). Anything else (consent wall, unexpected page) is unknown."""
    if "consent.youtube.com" in (final_url or ""):
        return "unknown"
    if re.search(r'"isLiveNow"\s*:\s*true', html):
        return "live"
    if re.search(r'"isLiveNow"\s*:\s*false', html):
        return "not_live"
    canon = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    if canon and "/channel/" in canon.group(1) and "/watch" not in canon.group(1):
        return "not_live"
    return "unknown"


def top_quality(html: str) -> int | None:
    """The tallest quality the /live page's player response offers (qualityLabel "1080p60" -> 1080), None when the page
    lists none (supervisor 2026-10-18: which channels offer 1080p+ at all)."""
    hs = [int(m) for m in re.findall(r'"qualityLabel"\s*:\s*"(\d+)p', html)]
    return max(hs) if hs else None


def yt_live_probe(handle: str, timeout: float = 12.0, errors: list | None = None) -> dict:
    """Fetch the channel's /live page: {state (live / not_live / unknown, fail open), top_height (top_quality)}."""
    try:
        req = urllib.request.Request(url_of(handle), headers=PROBE_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read(3_000_000).decode("utf-8", "replace")
            return {"state": classify_live_page(r.geturl(), html), "top_height": top_quality(html)}
    except Exception as e:
        if errors is not None:
            errors.append(repr(e)[:300])
        return {"state": "unknown", "top_height": None}


def yt_live_state(handle: str, timeout: float = 12.0, errors: list | None = None) -> str:
    """Fetch the channel's /live page and classify it; 'unknown' on any error (fail open; the error is appended to
    `errors` when given)."""
    return yt_live_probe(handle, timeout, errors)["state"]


def pick_live_channel(data_dir: Path, exclude: set[str] | None = None, probe=yt_live_state,
                      select=None) -> tuple[dict, list[dict]]:
    """next_channel(), re-drawn while the probe says the pick is positively not live. Returns (channel, pre-check
    skips). If every remaining channel probes not live, falls back to the first pick (the session then fails as
    before and counts a strike)."""
    exclude = set(exclude or ())
    first, skips = None, []
    for _ in range(len(YT_CHANNELS)):
        try:
            c = next_channel(data_dir, exclude=exclude, select=select)
        except RuntimeError:
            break
        first = first or c
        state = probe(c["handle"])
        if state != "not_live":
            return c, skips
        skips.append({"ts": time.time(), "handle": c["handle"], "type": c["type"], "role": c["role"], "state": state})
        exclude.add(c["handle"])
    if first is None:
        raise RuntimeError("no live channel left in the list")
    return first, skips


def live_now(handle: str) -> bool | None:
    """True / False from the probe, None when unknown (fail open: treat as maybe-live)."""
    return {"live": True, "not_live": False}.get(yt_live_state(handle))


# ---------------------------------------------------------------------------
# Spare promotion (prereg Addendum P): a spare takes its type's open slot only after isLiveNow is true at >= 3 checks
# >= 6 h apart spread over 24 h, logged and committed (yt_live_check.py appends to channel_checks/yt_live_checks.jsonl).
# Reading of "spread over 24 h": the passing checks span >= 24 h, and no not-live check falls inside that span;
# unknown probes (fail-open cases) are ignored.
# ---------------------------------------------------------------------------

PROMOTE_MIN_CHECKS = 3
PROMOTE_MIN_GAP_S = 6 * 3600
PROMOTE_SPAN_S = 24 * 3600
LIVE_CHECKS_LOG = Path(__file__).resolve().parent / "channel_checks" / "yt_live_checks.jsonl"


def promotion_status(checks: list[dict], handle: str) -> dict:
    """{'passed': bool, 'checks': [ts of the passing live checks]} from the live-check log rows of `handle`: the live
    checks after the last not-live one, taken greedily >= PROMOTE_MIN_GAP_S apart, pass at >= PROMOTE_MIN_CHECKS
    spanning >= PROMOTE_SPAN_S."""
    rows = sorted((r for r in checks if r.get("handle") == handle and r.get("state") in ("live", "not_live")),
                  key=lambda r: r["ts"])
    last_off = max((i for i, r in enumerate(rows) if r["state"] == "not_live"), default=-1)
    picked: list[float] = []
    for r in rows[last_off + 1:]:
        if not picked or r["ts"] - picked[-1] >= PROMOTE_MIN_GAP_S:
            picked.append(r["ts"])
    passed = len(picked) >= PROMOTE_MIN_CHECKS and picked[-1] - picked[0] >= PROMOTE_SPAN_S
    return {"passed": passed, "checks": picked}

