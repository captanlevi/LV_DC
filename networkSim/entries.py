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
NOT_LIVE_MARK = "Failed to navigate to a live"     # browser.py's start failure (yt / twitch: no live stream to enter)
CLASS_PRECEDENCE = ("not_live", "login_wall", "interrupted", "startup_failure", "room_ended", "player_dead",
                    "player_frozen", "qa_fail", "normal")


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
        out.append({"kind": "room_reentry", "t": te or tn, "url": r.get("room"), "rotation_ok": r.get("ok")})
    # levi 2026-10-17: a tt start failure (live rooms found, none played) is a failed, unshaped stream entry, even with
    # no ticks or a room that played briefly before the picker gave up on it
    start_failed = meta.get("platform") == "tiktok" and NOT_LIVE_MARK in (meta.get("abort_reason") or "")
    if start_failed and not any(e["kind"] == "stream" for e in out):
        out.insert(0, {"kind": "stream", "t": meta.get("started_at") or 0.0, "url": meta.get("channel_url")})
    for e in out:
        s = sustained_at(rows, e["t"])
        e.update(shaper_at_entry=shaper_at(net_labels, e["t"]), first_shaped_after=first_shaped_after(net_labels, e["t"]),
                 sustained_60s=s is not None, sustained_at=s)
        if start_failed and e["kind"] == "stream":
            e.update(sustained_60s=False, start_failed=True)
    return out


def dead_player_run(rows: list[dict]) -> float:
    """Longest run (s) of res_meta ticks with no media at all, after playback had been sustained once: readyState 0
    (ready_state column, from the 2026-10-17 extension) or, on older ticks, a 0x0 video at playhead 0."""
    played_at = sustained_at(rows, 0.0, window_s=float("inf"))
    longest, t0 = 0.0, None
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
            t0 = None
            continue
        t0 = ts if t0 is None else t0
        longest = max(longest, ts - t0)
    return longest


def session_class(platform: str, verdict: str, abort_reason: str | None, entries: list[dict],
                  dead_run_s: float | None, dead_t: float | None, frozen_s: float | None, frozen_t: float | None) -> str:
    """levi's precedence: not_live > login_wall > interrupted > startup_failure > room_ended > player_dead >
    player_frozen > qa_fail > normal. A start failure is not_live on yt / twitch (no live stream to enter) and a
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
    if start_failed or (stream is not None and not stream.get("sustained_60s")):
        return "startup_failure"
    if ab == "room_ended":
        return "room_ended"
    if ab == "player_dead" or (dead_t is not None and dead_run_s is not None and dead_run_s >= dead_t):
        return "player_dead"
    if ab == "player_frozen" or (frozen_t is not None and frozen_s is not None and frozen_s > frozen_t):
        return "player_frozen"
    if verdict != "PASS":
        return "qa_fail"
    return "normal"


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
    """net_labels.txt: one JSON object per line ({timestamp, speed, state})."""
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
