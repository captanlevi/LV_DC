"""entries.py: stream entries, their shaper states and whether playback was sustained (levi 2026-10-17, session_class
ruling). An entry is the stream entry (exp.py navigates and pre-rolls before the first shaper state, so it is always
unshaped) or a tt room re-entry (room rotation; a real shaped entry). Per entry: the shaper state at entry, the first
shaped state after it, and sustained_60s = a run of playing ticks with an advancing playhead lasting >= SUSTAIN_RUN_S
within SUSTAIN_WINDOW_S of the entry. The startup-failure rate is failed entries / entries per entry state.
Also session_class: one class per session by levi's fixed precedence (first match wins).
"""
from __future__ import annotations

SUSTAIN_WINDOW_S = 60.0
SUSTAIN_RUN_S = 5.0
# supervisor 2026-10-18 (yt_35): a stream entry that did not sustain play while the shaper already sat at state <= 2
# within the startup window is the shaper, not the platform: the class stays normal, check.json flags slow_start_shaped
# (a covariate); the entry still counts as failed in startup_rates (levi)
SLOW_START_MAX_STATE = 2
NOT_LIVE_MARK = "Failed to navigate to a live"     # browser.py's start failure (yt / twitch: no live stream to enter)
CLASS_PRECEDENCE = ("not_live", "login_wall", "interrupted", "startup_failure", "room_ended", "stream_ended", "player_dead",
                    "player_dead_recovered", "player_frozen", "player_frozen_recovered", "qa_fail", "normal")
# levi 2026-10-18 ruling (a) on the tt player_dead re-entry (peter's ti_37-39 read): the dead run that contains an
# outcome-continue re-entry's t_nav is not the player_dead verdict / class; it is reported as recovered_dead_s with its
# window (a covariate, labels unchanged). With no other FAIL the session reads this class: "normal" = (a), counts toward
# quotas; "player_dead_recovered" = option (b), kept and not counted. One recovery per session: a second recovered run
# excludes nothing (player_dead).
RECOVERED_DEAD_CLASS = "normal"
# the same treatment for a recovered frozen run (Q am. 6; levi's FROZEN_COLLECTOR audit F1, 2026-10-18), under its own
# name and switch: "normal" counts toward quotas, "player_frozen_recovered" is kept and not counted
RECOVERED_FROZEN_CLASS = "normal"
RECOVERY_SLACK_S = 2.0      # t_nav may fall this far outside the run's first / last dead tick (tick spacing ~1 s)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _true(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "1.0")


def tick_states(rows: list[dict]) -> list[tuple[dict, bool]]:
    """(res_meta tick, stall label on) in order; the label from the event stream (stall ... start / video_start)."""
    out, stall = [], False
    for r in rows:
        ty = r.get("type")
        if ty == "stall":
            stall = True
        elif ty in ("start", "video_start"):
            stall = False
        elif ty == "res_meta":
            out.append((r, stall))
    return out


def sustained_at(rows: list[dict], t: float, window_s: float = SUSTAIN_WINDOW_S, run_s: float = SUSTAIN_RUN_S,
                 ads_count: bool = False) -> float | None:
    """Time (s) at which a run of playing ticks (stall label off, not an ad, not paused) with the playhead advancing
    tick to tick first reached run_s, if within window_s of t; else None. ads_count (restart prereg fe48beec, boundary
    bundle item 5; the entries' startup reading only): an unpaused ad tick with the playhead advancing is play too
    (tw_100051: a 70 s preroll read as a startup failure)."""
    start, prev_ct = None, None
    for r, stall in tick_states(rows):
        ts, ct = _f(r.get("ts")), _f(r.get("current_time"))
        if ts is None:
            continue
        ts /= 1000.0
        if ts < t:
            prev_ct = ct
            continue
        if ts > t + window_s:
            return None
        playing = (not stall and (ads_count or not _true(r.get("is_ad"))) and not _true(r.get("paused"))
                   and ct is not None and prev_ct is not None and ct > prev_ct)
        if playing:
            start = ts if start is None else start
            if ts - start >= run_s:
                return ts
        else:
            start = None
        prev_ct = ct
    return None


def first_tick_at(rows: list[dict], url: str | None, after: float = 0.0) -> float | None:
    """The first res_meta tick (s) on `url` at or after `after`."""
    for r in rows:
        if r.get("type") == "res_meta" and (url is None or r.get("url") == url):
            ts = _f(r.get("ts"))
            if ts is not None and ts / 1000.0 >= after:
                return ts / 1000.0
    return None


def shaper_at(net_labels: list[dict], t: float) -> dict:
    """The shaper state in force at t (the last net label at or before t), or unshaped."""
    cur = None
    for n in sorted(net_labels, key=lambda n: n["timestamp"]):
        if n["timestamp"] <= t:
            cur = n
    return {"state": str(cur["state"]), "rate_kbit": cur["speed"]} if cur else {"state": "unshaped", "rate_kbit": None}


def first_shaped_after(net_labels: list[dict], t: float) -> dict | None:
    n = next((n for n in sorted(net_labels, key=lambda n: n["timestamp"]) if n["timestamp"] > t), None)
    return {"state": str(n["state"]), "rate_kbit": n["speed"], "t": n["timestamp"]} if n else None


def compute_entries(rows: list[dict], meta: dict, net_labels: list[dict]) -> list[dict]:
    """The session's entries (module docstring). The stream entry is the first tick on the entered stream's URL
    (meta channel_url), else the first tick; tt re-entries are the room rotation's moves (first tick on the new room
    after t_enter / t_nav)."""
    out = []
    rooms = (meta.get("room_rotation") or {}).get("rooms") or []
    t0 = first_tick_at(rows, meta.get("channel_url")) or first_tick_at(rows, None)
    if t0 is not None:
        out.append({"kind": "stream", "t": t0, "url": meta.get("channel_url")})
    for r in rooms:
        if r.get("first") or not (r.get("t_enter") or r.get("t_nav")):
            continue
        tn = r.get("t_enter") or r["t_nav"]
        te = first_tick_at(rows, r.get("room"), after=tn) if r.get("room") else None
        out.append({"kind": "room_reentry", "t": te or tn, "url": r.get("room"), "rotation_ok": r.get("ok"),
                    "reason": r.get("reason", "rotation"),
                    **({"dead_window": r.get("dead_window"), "outcome": r.get("outcome")} if r.get("reason") == "player_dead" else {}),
                    **({"outcome": r.get("outcome")} if r.get("reason") == "player_frozen" else {})})
    # Addendum Q am. 6: the stuck-player re-entry of the same channel (yt / twitch; tt's is a room re-entry above)
    if meta.get("platform") != "tiktok":
        for r in meta.get("frozen_reentries") or []:
            tn = r.get("t_enter") or r.get("t_nav")
            if tn:
                out.append({"kind": "reentry", "t": first_tick_at(rows, None, after=tn) or tn, "url": r.get("url"),
                            "reason": "player_frozen", "outcome": r.get("outcome")})
    # levi 2026-10-17: a tt start failure (live rooms found, none played) is a failed, unshaped stream entry, even with
    # no ticks or a room that played briefly before the picker gave up on it
    start_failed = meta.get("platform") == "tiktok" and NOT_LIVE_MARK in (meta.get("abort_reason") or "")
    if start_failed and not any(e["kind"] == "stream" for e in out):
        out.insert(0, {"kind": "stream", "t": meta.get("started_at") or 0.0, "url": meta.get("channel_url")})
    # Addendum Q am. 5 / 6: a censored stall that began within 60 s of an entry fails that entry
    cen = meta.get("censored") or {}
    stall0 = (cen.get("frozen_window") or [None])[0]
    for e in out:
        s = sustained_at(rows, e["t"], ads_count=True)      # fe48beec: ad play sustains the entry
        e.update(shaper_at_entry=shaper_at(net_labels, e["t"]), first_shaped_after=first_shaped_after(net_labels, e["t"]),
                 sustained_60s=s is not None, sustained_at=s)
        if start_failed and e["kind"] == "stream":
            e.update(sustained_60s=False, start_failed=True)
        if stall0 is not None and 0 <= stall0 - e["t"] <= SUSTAIN_WINDOW_S:
            e.update(sustained_60s=False, censored_stall=True)
    return out


def nav_times(meta: dict) -> list[float]:
    """levi 11:40 (ti_100086 / ti_100090): every navigation of ours inside the session, sorted: the re-entries' t_nav
    (player_dead_reentries, frozen_reentries, the room rotation's moves past the first room; t_enter where a record has
    no t_nav). The collector's watches are rebuilt there, so no clock of theirs runs across one."""
    recs = list(meta.get("player_dead_reentries") or []) + list(meta.get("frozen_reentries") or [])
    recs += [r for r in (meta.get("room_rotation") or {}).get("rooms") or [] if not r.get("first")]
    out = set()
    for r in recs:
        t = r.get("t_nav") if isinstance(r.get("t_nav"), (int, float)) else r.get("t_enter")
        if isinstance(t, (int, float)):
            out.add(float(t))
    return sorted(out)


def nav_loads(meta: dict) -> list[tuple[float, float]]:
    """Our navigations that came back (nav_times' set with a t_playing): (t_nav, t_playing). The dead ticks between are
    the new page loading after our own navigation, never a dead player (as the labeler censors t_nav -> video_start)."""
    recs = list(meta.get("player_dead_reentries") or []) + list(meta.get("frozen_reentries") or [])
    recs += [r for r in (meta.get("room_rotation") or {}).get("rooms") or [] if not r.get("first")]
    out = set()
    for r in recs:
        t = r.get("t_nav") if isinstance(r.get("t_nav"), (int, float)) else r.get("t_enter")
        if isinstance(t, (int, float)) and isinstance(r.get("t_playing"), (int, float)) and r["t_playing"] >= t:
            out.add((float(t), float(r["t_playing"])))
    return sorted(out)


def dead_tick_runs(rows: list[dict], navs: list[float] | None = None) -> list[list[float]]:
    """dead_runs' runs as their dead ticks' times (s). With navs (nav_times; levi 11:40): a run is split at each of our
    navigations inside it: the part before ends AT the navigation (its censored end, appended as its last time: the
    collector's clock ran to the rebuild) and the ticks from the navigation on (the new page loading) are a run of
    their own."""
    navs = sorted(navs or [])
    played_at = sustained_at(rows, 0.0, window_s=float("inf"))
    runs, cur = [], []
    for r, _ in tick_states(rows):
        ts = _f(r.get("ts"))
        if ts is None:
            continue
        ts /= 1000.0
        rs = r.get("ready_state")
        if rs not in (None, "", "None", "null"):
            dead = _f(rs) == 0
        else:
            dead = _f(r.get("video_w")) in (0.0, None) and _f(r.get("current_time")) in (0.0, None)
        if not dead or played_at is None or ts < played_at:
            if cur:
                runs.append(cur)
            cur = []
            continue
        cut = next((n for n in navs if cur and cur[-1] < n <= ts), None)
        if cut is not None:
            runs.append(cur + [cut])
            cur = []
        cur.append(ts)
    if cur:
        runs.append(cur)
    return runs


def dead_open_at_end(rows: list[dict], navs: list[float] | None = None) -> bool:
    """A dead run with no live res_meta tick after it: the page never came back before the session ended."""
    runs = dead_tick_runs(rows, navs)
    if not runs:
        return False
    last = runs[-1][-1]
    return not any(_f(r.get("ts")) is not None and float(r["ts"]) / 1000.0 > last for r, _ in tick_states(rows))


def held_dead_runs(rows: list[dict], net_labels: list, navs: list[float] | None = None) -> list[tuple[float, float]]:
    """The dead runs with the parts the collector's player_dead clock holds left out (bd4bebf: shaper states 1-4 and
    stuck.LOW_HOLD_GRACE_S after leaving them; the clock restarts after the hold): (first, last tick) per remaining
    part, as dead_runs measures (levi 09:40 / 11:22, the end-of-day-2 tip: the class follows the collector's hold)."""
    import stuck
    spans = stuck.low_spans(net_labels)
    held = lambda t: any(a <= t <= b + stuck.LOW_HOLD_GRACE_S for a, b in spans)
    out = []
    for run in dead_tick_runs(rows, navs):
        seg = []
        for t in run:
            if held(t):
                if seg:
                    out.append((seg[0], seg[-1]))
                seg = []
            else:
                seg.append(t)
        if seg:
            out.append((seg[0], seg[-1]))
    return out


def held_frozen_s(runs: list[tuple[float, float]], net_labels: list, step_s: float = 1.0) -> float:
    """The longest frozen run as the collector's held addendum-N clock counts it (browser._on_frozen from 032f6d31, levi
    gate 49d (C)): at each second t of a run (first, last tick) the clock reads 0 while stuck.low_hold_end(t) holds (states
    1-4 + stuck.LOW_HOLD_GRACE_S), else min(t - first, t - hold end); the maximum over every run and second."""
    import stuck
    best = 0.0
    for a, b in runs:
        ts = [a + k * step_s for k in range(int((b - a) / step_s) + 1)] + [b]
        for t in ts:
            he = stuck.low_hold_end(net_labels, t)
            c = t - a if he is None else (0.0 if t <= he else min(t - a, t - he))
            best = max(best, c)
    return best


def open_dead_run_held_s(rows: list[dict], net_labels: list, navs: list[float] | None = None) -> float | None:
    """levi 03:51 (ti_100084): the length of the dead run still open at the last tick, as the collector's clock counts
    it: its final part outside the low-state hold (states 1-4 + grace), first to last dead tick; None when no run is
    open at the end. The session's other, earlier runs never enter it."""
    if not dead_open_at_end(rows, navs):
        return None
    import stuck
    spans = stuck.low_spans(net_labels or [])
    run = dead_tick_runs(rows, navs)[-1]
    seg: list[float] = []
    for t in run:
        if any(a <= t <= b + stuck.LOW_HOLD_GRACE_S for a, b in spans):
            seg = []
        else:
            seg.append(t)
    return (seg[-1] - seg[0]) if seg else 0.0


def unresolved_dead_runs(rows: list[dict], open_clocks: list[dict], low_spans: list, t_dead: float | None,
                         net_labels: list | None = None, navs: list[float] | None = None,
                         shared_navs: list[float] | None = None) -> tuple[list[dict], list[tuple[float, float]]]:
    """levi 03:58 / 04:06 (final, (H)): for each dead clock left unresolved (end None), the raw dead run it belongs to
    (dead_tick_runs: the run holding the clock's start, else the first run starting after it) and, as the mask, that
    run's HELD parts (held_dead_runs: the parts outside the low-state hold, states 1-4 + grace, the measure check's
    dead class uses): the held-out ticks are a labelled STALL by the hold's rule, so only the parts the collector's clock
    should have counted are uncertain. (provisional runs, hard runs): a raw run >= t_dead with no low span (watch
    low_spans, + grace) inside it is hard (player_dead as before, not exported); every other one exports each held part
    as {onset, end, held_s, raw_onset, raw_end, raw_s, low_span}. Times are epoch seconds (labels.csv ts / 1000, the
    clock the net labels and meta use), never slot indices; end / raw_end None = open at the session's last tick.
    brad's cuts loader masks every slot overlapping [onset, end) of each item, on its own slot grid."""
    import stuck
    runs = dead_tick_runs(rows, navs)
    parts = held_dead_runs(rows, net_labels or [], navs)
    last_tick = max((float(r["ts"]) / 1000.0 for r, _ in tick_states(rows) if _f(r.get("ts")) is not None), default=None)
    open_end = dead_open_at_end(rows, navs)
    prov, hard, seen = [], [], set()
    for c in open_clocks:
        t = c.get("start")
        if not isinstance(t, (int, float)):
            continue
        run = next((x for x in runs if x[0] - 1.0 <= t <= x[-1] + 1.0), None) or next((x for x in runs if x[0] >= t), None)
        if run is None or (run[0], run[-1]) in seen:
            continue
        seen.add((run[0], run[-1]))
        a, b = run[0], run[-1]
        is_open = open_end and run is runs[-1]
        raw_b = max(b, last_tick) if is_open and last_tick is not None else b
        low_inside = any(lo <= raw_b and a <= hi + stuck.LOW_HOLD_GRACE_S for lo, hi in low_spans)
        if t_dead is not None and raw_b - a >= t_dead and not low_inside and not _ends_at(b, shared_navs):
            hard.append((a, raw_b))
            continue
        for pa, pb in parts:
            if a <= pa and pb <= b:
                end_open = is_open and pb == b
                pe = raw_b if end_open else pb
                prov.append({"onset": round(pa, 3), "end": None if end_open else round(pb, 3),
                             "held_s": round(pe - pa, 3), "raw_onset": round(a, 3),
                             "raw_end": None if is_open else round(b, 3), "raw_s": round(raw_b - a, 3),
                             "low_span": low_inside})
    return prov, hard


def dead_runs(rows: list[dict], navs: list[float] | None = None) -> list[tuple[float, float]]:
    """Runs (first, last dead tick, s) of res_meta ticks with no media at all, after playback had been sustained once:
    readyState 0 (ready_state column, from the 2026-10-17 extension) or, on older ticks, a 0x0 video at playhead 0.
    dead_tick_runs' runs (navs: split at our navigations, levi 11:40)."""
    return [(r[0], r[-1]) for r in dead_tick_runs(rows, navs)]


def dead_player_run(rows: list[dict]) -> float:
    """Longest dead run (s), recovered or not."""
    return max((b - a for a, b in dead_runs(rows)), default=0.0)


def _ends_at(end: float, navs) -> bool:
    return any(abs(end - n) < 1e-6 for n in navs or ())


def dead_player(rows: list[dict], reentries: list[dict] | None, net_labels: list | None = None,
                navs: list[float] | None = None, shared_navs: list[float] | None = None,
                loads: list[tuple[float, float]] | None = None) -> dict:
    """The player_dead measure with levi's recovery rule (RECOVERED_DEAD_CLASS): {max_s (longest run not recovered),
    max_all_s, recovered (recovered_dead_s, window, t_nav, t_playing, dead_window) or None, recovered_runs}. With
    net_labels (tt), the runs are held_dead_runs: the parts the collector's low-state hold held are left out."""
    runs = dead_runs(rows, navs)
    cont = [r for r in reentries or [] if r.get("outcome") == "continue" and r.get("t_nav")]
    # with navs the run our re-entry ended ends at its t_nav; the new page's load run after it is not that run
    rec = [(run, r) for run in runs for r in cont
           if run[0] - RECOVERY_SLACK_S <= r["t_nav"] <= run[1] + RECOVERY_SLACK_S and (navs is None or run[0] < r["t_nav"])]
    keep = runs if len(rec) != 1 else [x for x in runs if x != rec[0][0]]
    # the recovery is matched on the raw runs (the run our re-entry ended); the measure, with net labels, on the parts
    # of the runs the low-state hold did not hold
    parts = held_dead_runs(rows, net_labels, navs) if net_labels is not None else runs
    inside = lambda p, rs: any(a <= p[0] and p[1] <= b for a, b in rs)
    # levi 14:43: a run our own re-entry ends at the shared deadline (shared_navs: check_session's tag) is not compared
    # with T; its censored length is reported (shared_deadline_runs), its class follows the re-entry's outcome
    shared = [(a, b) for a, b in parts if _ends_at(b, shared_navs)]
    # the new page loading after our own navigation (nav_loads: t_nav -> t_playing) is no dead run of the player
    load = [(a, b) for a, b in parts if any(n <= a and b <= tp + RECOVERY_SLACK_S for n, tp in loads or ())]
    parts = [x for x in parts if x not in load]
    out = {"max_s": max((b - a for a, b in parts if inside((a, b), keep) and (a, b) not in shared), default=0.0),
           "max_all_s": max((b - a for a, b in parts), default=0.0),
           "reentry_load_runs": [{"onset": round(a, 3), "end": round(b, 3), "s": round(b - a, 3)} for a, b in load],
           "recovered": None, "recovered_runs": len(rec),
           "shared_deadline_runs": [{"onset": round(a, 3), "censored_end": round(b, 3), "s": round(b - a, 3)}
                                    for a, b in shared]}
    if len(rec) == 1:
        (a, b), r = rec[0]
        out["recovered"] = {"recovered_dead_s": round(b - a, 1), "window": [a, b], "t_nav": r["t_nav"],
                            "t_playing": r.get("t_playing"), "dead_window": r.get("dead_window")}
    return out


def dead_over_t(run_s: float | None, t: float | None) -> bool:
    """The player_dead boundary (Q am. 12): a dead run fails at >= T."""
    return t is not None and run_s is not None and run_s >= t


def frozen_over_t(run_s: float | None, t: float | None) -> bool:
    """The player_frozen boundary (Q am. 12): a frozen run fails above T (> T)."""
    return t is not None and run_s is not None and run_s > t


def session_class(platform: str, verdict: str, abort_reason: str | None, entries: list[dict],
                  dead_run_s: float | None, dead_t: float | None, frozen_s: float | None, frozen_t: float | None,
                  recovered_dead: bool = False, recovered_frozen: bool = False) -> str:
    """levi's precedence (Q am. 12): not_live > login_wall > interrupted > startup_failure > room_ended > stream_ended
    (boundary bundle item 3) > player_dead >
    (player_dead_recovered) > player_frozen > (player_frozen_recovered) > qa_fail > normal; the recovered classes only
    when their RECOVERED_*_CLASS switch is not "normal". Boundaries: dead at >= T, frozen at > T (Q am. 12). A start failure is not_live on yt / twitch (no live stream to enter) and a
    startup_failure on tt (live rooms were found but none played)."""
    ab = abort_reason or ""
    start_failed = NOT_LIVE_MARK in ab
    if start_failed and platform in ("youtube", "twitch"):
        return "not_live"
    if ab == "login_wall":
        return "login_wall"
    if ab.startswith(("signal", "interrupted")):
        return "interrupted"
    stream = next((e for e in entries if e.get("kind") == "stream"), None)
    if start_failed or (stream is not None and not stream.get("sustained_60s") and not slow_start_shaped(entries)):
        return "startup_failure"
    if ab == "room_ended":
        return "room_ended"
    if ab == "stream_ended":               # boundary bundle item 3: yt / twitch, the tab left its live stream
        return "stream_ended"
    if ab in ("player_dead", "player_error") or dead_over_t(dead_run_s, dead_t):   # player_error: yt / twitch (t56)
        return "player_dead"
    if recovered_dead and RECOVERED_DEAD_CLASS != "normal":
        return RECOVERED_DEAD_CLASS
    if ab == "player_frozen" or frozen_over_t(frozen_s, frozen_t):
        return "player_frozen"
    if recovered_frozen and RECOVERED_FROZEN_CLASS != "normal":
        return RECOVERED_FROZEN_CLASS
    if verdict != "PASS":
        return "qa_fail"
    return "normal"


def slow_start_shaped(entries: list[dict]) -> bool:
    """The stream entry did not sustain play within SUSTAIN_WINDOW_S, and the shaper's first state after the entry, applied
    inside that window, was <= SLOW_START_MAX_STATE: a slow start caused by the shaper, not a startup failure."""
    s = next((e for e in entries if e.get("kind") == "stream"), None)
    if s is None or s.get("sustained_60s"):
        return False
    f = s.get("first_shaped_after") or {}
    try:
        state = int(f.get("state"))
    except (TypeError, ValueError):
        return False
    t0, tf = s.get("t"), f.get("t")
    if t0 is not None and tf is not None and tf - t0 > SUSTAIN_WINDOW_S:
        return False
    return state <= SLOW_START_MAX_STATE


def startup_rates(entries: list[dict]) -> dict:
    """Failed entries / entries per (kind, entry state), with counts."""
    out: dict = {}
    for e in entries:
        k = f"{e.get('kind')}:{(e.get('shaper_at_entry') or {}).get('state')}"
        d = out.setdefault(k, {"entries": 0, "failed": 0})
        d["entries"] += 1
        d["failed"] += not e.get("sustained_60s")
    for d in out.values():
        d["rate"] = d["failed"] / d["entries"]
    return out


def read_net_labels(sd) -> list[dict]:
    """net_labels.txt: one JSON object per line ({timestamp, speed, state}; from restart §2 also the realised delay /
    loss, the sub-step's re-draw and walk_step: utils/dataModels.NetLabel). Every row is a shaper change."""
    import json
    from pathlib import Path
    p = Path(sd) / "net_labels.txt"
    if not p.exists():
        return []
    out = []
    for l in p.read_text().splitlines():
        try:
            out.append(json.loads(l))
        except Exception:
            continue
    return out


def write_entries(sd) -> list[dict]:
    """exp.py, after labels.csv and net_labels.txt are written: the session's entries into meta.json `entries`."""
    import csv
    import json
    from pathlib import Path
    sd = Path(sd)
    rows = list(csv.DictReader(open(sd / "labels.csv", newline=""))) if (sd / "labels.csv").exists() else []
    meta = json.loads((sd / "meta.json").read_text()) if (sd / "meta.json").exists() else {}
    ents = compute_entries(rows, meta, read_net_labels(sd))
    meta["entries"] = ents
    (sd / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    return ents
