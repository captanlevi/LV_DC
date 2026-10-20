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
CLASS_PRECEDENCE = ("not_live", "login_wall", "interrupted", "startup_failure", "room_ended", "player_dead",
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


def sustained_at(rows: list[dict], t: float, window_s: float = SUSTAIN_WINDOW_S, run_s: float = SUSTAIN_RUN_S) -> float | None:
    """Time (s) at which a run of playing ticks (stall label off, not an ad, not paused) with the playhead advancing
    tick to tick first reached run_s, if within window_s of t; else None."""
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
        playing = (not stall and not _true(r.get("is_ad")) and not _true(r.get("paused"))
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
        s = sustained_at(rows, e["t"])
        e.update(shaper_at_entry=shaper_at(net_labels, e["t"]), first_shaped_after=first_shaped_after(net_labels, e["t"]),
                 sustained_60s=s is not None, sustained_at=s)
        if start_failed and e["kind"] == "stream":
            e.update(sustained_60s=False, start_failed=True)
        if stall0 is not None and 0 <= stall0 - e["t"] <= SUSTAIN_WINDOW_S:
            e.update(sustained_60s=False, censored_stall=True)
    return out


def dead_runs(rows: list[dict]) -> list[tuple[float, float]]:
    """Runs (first, last dead tick, s) of res_meta ticks with no media at all, after playback had been sustained once:
    readyState 0 (ready_state column, from the 2026-10-17 extension) or, on older ticks, a 0x0 video at playhead 0."""
    played_at = sustained_at(rows, 0.0, window_s=float("inf"))
    runs, t0, last = [], None, None
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
            if t0 is not None:
                runs.append((t0, last))
            t0 = None
            continue
        t0 = ts if t0 is None else t0
        last = ts
    if t0 is not None:
        runs.append((t0, last))
    return runs


def dead_player_run(rows: list[dict]) -> float:
    """Longest dead run (s), recovered or not."""
    return max((b - a for a, b in dead_runs(rows)), default=0.0)


def dead_player(rows: list[dict], reentries: list[dict] | None) -> dict:
    """The player_dead measure with levi's recovery rule (RECOVERED_DEAD_CLASS): {max_s (longest run not recovered),
    max_all_s, recovered (recovered_dead_s, window, t_nav, t_playing, dead_window) or None, recovered_runs}."""
    runs = dead_runs(rows)
    cont = [r for r in reentries or [] if r.get("outcome") == "continue" and r.get("t_nav")]
    rec = [(run, r) for run in runs for r in cont
           if run[0] - RECOVERY_SLACK_S <= r["t_nav"] <= run[1] + RECOVERY_SLACK_S]
    keep = runs if len(rec) != 1 else [x for x in runs if x != rec[0][0]]
    out = {"max_s": max((b - a for a, b in keep), default=0.0), "max_all_s": max((b - a for a, b in runs), default=0.0),
           "recovered": None, "recovered_runs": len(rec)}
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
    """levi's precedence (Q am. 12): not_live > login_wall > interrupted > startup_failure > room_ended > player_dead >
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
