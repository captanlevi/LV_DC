#!/usr/bin/env python3
"""
Orchestrate multi-session QoE data collection with adaptive state targeting.

After each completed session it analyzes the state distribution and picks the
most under-represented state as the forced starting point for the next session,
gradually driving the dataset toward a uniform distribution across all 10 states. Collection v2: yt under a t38
matrix (A / B) is not forced (its start is drawn from the matrix's stationary distribution, which is its balance
target), and each yt session goes to the next channel in the yt_channels.py rotation.

Usage:
    # Collect one session for YouTube (prompts you to open a live stream first)
    python orchestrate.py --platform youtube

    # Collect 5 back-to-back sessions, targeting balance automatically
    python orchestrate.py --platform youtube --runs 5

    # Run sessions for every platform in round-robin until N sessions each
    python orchestrate.py --all-platforms --runs 5

    # Collection v2 batch (resumable; see run_batch)
    python orchestrate.py --batch youtube=30,twitch=15,tiktok=15

    # Just print the current balance without collecting anything
    python orchestrate.py --platform youtube --report-only
    python orchestrate.py --all-platforms --report-only
"""
import os
import subprocess
import sys
import argparse
import json
import shutil
import time
import datetime as dt
from pathlib import Path

from balance import PLATFORM_TRANSITIONS, most_needed_state, balance_report
import batch_summary
import coverage_report
import geometry
import preflight
import tw_categories
import yt_channels

_RUNS_DIR = Path(__file__).parent.parent / "data"

EXIT_LOGIN_WALL = 3   # exp.py's exit code for a session aborted at a login wall (t37)
EXIT_STOPPED = 130    # exp.py's exit code when the operator stopped the session
EXIT_PLAYER_FROZEN = 5   # exp.py's exit code when the watchdog aborted a frozen player (addendum N)
EXIT_ROOM_ENDED = 6      # exp.py's exit code when a tt room died mid-session (room_ended)
EXIT_PLAYER_DEAD = 7     # exp.py's exit code when the tt player sat at readyState 0 with no media (player_dead)
# aborts that are not failures of the platform: they do not count toward MAX_CONSECUTIVE_FAIL
NOT_A_FAILURE = {EXIT_PLAYER_FROZEN: "player_frozen", EXIT_ROOM_ENDED: "room_ended", EXIT_PLAYER_DEAD: "player_dead"}

EXIT_LOW_DISK = 4     # orchestrate.py's own exit code when the data volume has less than MIN_FREE_GB free
EXIT_PREFLIGHT = 8    # orchestrate.py's own exit code when the per-session preflight fails (preflight.py)

# collection v2 (LVQoE prereg t36 §1): episodes per session are fixed per platform, not left to the operator
EPISODES_V2 = {"youtube": 21, "tiktok": 23, "twitch": 23}

# --batch (t37, week-long run): stop before a session when the data volume has less than this share free; pause a
# platform after this many consecutive sessions without a PASS; a yt channel that failed to start is skipped this long
# absolute, not a share (supervisor 2026-10-17: 10 % of chaos's 932 GB disk stopped ~70 GB early at ~5 GB/h)
MIN_FREE_GB = 25
MAX_CONSECUTIVE_FAIL = 5
# NOT_A_FAILURE aborts (player_frozen, room_ended, player_dead) do not count toward MAX_CONSECUTIVE_FAIL; a separate cap stops a
# platform that keeps aborting this way from looping forever
MAX_CONSECUTIVE_FROZEN = 5
NOT_LIVE_RETRY_S = 6 * 3600
BATCH_ORDER = ("youtube", "twitch", "tiktok", "bilibili")
# furthest-behind must not starve the others (supervisor 2026-10-17: tt at 4/80 took every session for an hour): at
# most this many sessions in a row on one platform, then the furthest-behind of the others
MAX_CONSECUTIVE_SAME_PLATFORM = 2
# staleness floor (supervisor 2026-10-17: with tt and twitch both far behind, the cap alternated them and yt did not run
# for 7.5 h): a platform absent from the last STALE_AFTER_SESSIONS sessions goes next
STALE_AFTER_SESSIONS = 4


def next_platform(todo: list[str], ratio, recent: list[str], cap: int = MAX_CONSECUTIVE_SAME_PLATFORM,
                  stale_after: int = STALE_AFTER_SESSIONS) -> str:
    """Next platform from `todo`, given `recent` (the platforms of the latest sessions, oldest first):
    1. staleness floor: a platform absent from the last `stale_after` sessions goes first (the furthest behind of them);
    2. otherwise the furthest-behind platform (ratio(p) = accepted / target; BATCH_ORDER breaks ties), unless it has just
       run `cap` sessions in a row and another platform is still to do."""
    order = sorted(todo, key=lambda q: (ratio(q), BATCH_ORDER.index(q)))
    if len(recent) >= stale_after:
        stale = [q for q in order if q not in recent[-stale_after:]]
        if stale:
            return stale[0]
    last = recent[-1] if recent else None
    streak = 0
    for q in reversed(recent):
        if q != last:
            break
        streak += 1
    if last in todo and streak >= cap:
        others = [q for q in order if q != last]
        if others:
            return others[0]
    return order[0]


def recent_platforms(runs_dir: Path, n: int = 10) -> list[str]:
    """Platforms of the last n sessions on disk (PASS and _failed, by meta.json started_at), oldest first, so the cap
    and the staleness floor survive an orchestrate restart."""
    rows = []
    for plat in BATCH_ORDER:
        for d in list((runs_dir / plat).glob("*")) + list((runs_dir / "_failed" / plat).glob("*")):
            m = d / "meta.json"
            try:
                t = float(json.loads(m.read_text()).get("started_at") or m.stat().st_mtime)
            except Exception:
                continue
            rows.append((t, plat))
    return [p for _, p in sorted(rows)[-n:]]


def _prompt_ready(platform: str, initial_state: str | None) -> bool:
    state_hint = f"starting at state {initial_state}" if initial_state else "weighted-random start"
    print(f"\n  Platform : {platform.upper()}")
    print(f"  Strategy : {state_hint}")
    print(f"\n  Open {platform.capitalize()} Live in Chrome and navigate to a live stream.")
    ans = input("  Press Enter when ready  (or 'q' to abort this run): ").strip().lower()
    return ans != "q"


ENTRY_QUALITY = False   # --entry-quality: pass exp.py --entry-quality (Q am. 1 / am. 10; yt / twitch; default off)
DPMS_OFF = False   # --dpms-off: pass exp.py --dpms-off (chaos: re-blank the monitor after each session's window resize)
# shaper matrix per platform (yt t38; tt / twitch prereg addendum M) and the tt room-rotation fallback (seconds or None)
MATRIX_IDS: dict[str, str] = {"youtube": "A", "tiktok": "TT-M", "twitch": "TW-M"}
# --batch matrix per session (prereg Addendum Q, 2026-10-18): "coverage" = coverage_report.choose() on the accepted
# sessions' labels (deterministic rule, recorded in meta.json coverage); "fixed" = MATRIX_IDS. Allowed set per platform:
# every defined matrix (with Addendum M's alone, tt and twitch would have a single matrix and no choice)
MATRIX_RULE = "coverage"
COVERAGE_ALLOWED: dict[str, list[str]] = {}
TT_ROOM_ROTATION_S: float | None = None


def _collection_cmd(platform: str, initial_state: str | None, episodes: int, auto: bool = False,
                    yt_matrix: str | None = None, channel: dict | None = None, geom: str | None = None,
                    tw_category: dict | None = None, matrix: str | None = None) -> list[str]:
    cmd = [
        sys.executable, "exp.py",
        "--platform", platform,
        "--episodes", str(episodes),
    ]
    if initial_state:
        cmd += ["--initial-state", initial_state]
    if auto:
        cmd += ["--auto"]
    if platform == "youtube" and (matrix or yt_matrix):
        cmd += ["--yt-matrix", matrix or yt_matrix]
    if platform == "tiktok":
        cmd += ["--tt-matrix", matrix or MATRIX_IDS["tiktok"]]
        if TT_ROOM_ROTATION_S:
            cmd += ["--tt-room-rotation", str(TT_ROOM_ROTATION_S)]
    if platform == "twitch":
        cmd += ["--tw-matrix", matrix or MATRIX_IDS["twitch"]]
    if channel:
        cmd += ["--channel-url", yt_channels.url_of(channel["handle"]), "--channel-handle", channel["handle"],
                "--channel-type", channel["type"]]
    if tw_category:
        cmd += ["--tw-category", tw_category["name"]]
        if tw_category.get("channel"):
            cmd += ["--tw-channel", tw_category["channel"]]
    if geom:
        cmd += ["--geometry", geom]
    if DPMS_OFF:
        cmd += ["--dpms-off"]
    if ENTRY_QUALITY and platform in ("youtube", "twitch"):
        cmd += ["--entry-quality"]
    return cmd


def _run_collection(*a, **kw) -> int:
    cmd = _collection_cmd(*a, **kw)
    print(f"\n  Running: {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def _collect_platform(platform: str, runs: int, episodes_per_run: int, auto: bool = False,
                      yt_matrix: str | None = None) -> None:
    not_live: set[str] = set()   # yt channels whose session failed to start this invocation (tried once each)
    for i in range(runs):
        print(f"\n[{platform.upper()} — run {i+1}/{runs}]")
        target_state = most_needed_state(platform, _RUNS_DIR, yt_matrix=yt_matrix, matrix_id=MATRIX_IDS.get(platform))
        if platform == "youtube":
            for r in yt_channels.apply_leave_rule(_RUNS_DIR, time.time()):
                print(f"  LIST: {r['handle']} leaves the yt list ({r['evidence']}; Addendum P); commit it in yt_channels.py")
        channel = (yt_channels.next_channel(_RUNS_DIR, exclude=not_live | set(yt_channels.not_live_skips(_RUNS_DIR, time.time())))
                   if platform == "youtube" else None)
        if channel:
            print(f"  Channel  : {channel['handle']} ({channel['type']}, {channel['role']})")
        if not auto and not _prompt_ready(platform, target_state):
            print("  Skipped.")
            continue
        rc = _run_collection(platform, initial_state=target_state, episodes=episodes_per_run, auto=auto,
                             yt_matrix=yt_matrix, channel=channel)
        if rc == EXIT_STOPPED:
            print("  Session stopped by the operator (SIGINT / SIGTERM): stopping the batch.")
            sys.exit(EXIT_STOPPED)
        if rc == EXIT_LOGIN_WALL:
            print(f"  LOGIN WALL on {platform}: stopping this platform (fix the profile template, then rerun).")
            balance_report(platform, _RUNS_DIR, yt_matrix=yt_matrix, matrix_id=MATRIX_IDS.get(platform))
            return
        if rc in NOT_A_FAILURE:
            print(f"  {NOT_A_FAILURE[rc]}: session kept in _failed/, next run re-picks.")
        elif rc != 0:
            print("  Collection returned non-zero — check logs above.")
            if channel:
                not_live.add(channel["handle"])
        balance_report(platform, _RUNS_DIR, yt_matrix=yt_matrix, matrix_id=MATRIX_IDS.get(platform))


def accepted_sessions(platform: str, runs_dir: Path | None = None) -> int:
    """PASS sessions on disk (data/<platform>/*/check.json); a FAIL is archived to data/_failed and not counted. The
    batch recounts these at every start, so a restart resumes where it stopped. runs_dir: the trio's own root."""
    n = 0
    for c in ((runs_dir or _RUNS_DIR) / platform).glob("*/check.json"):
        try:
            n += json.loads(c.read_text()).get("verdict") == "PASS"
        except Exception:
            pass
    return n


def free_share() -> float:
    du = shutil.disk_usage(_RUNS_DIR if _RUNS_DIR.exists() else _RUNS_DIR.parent)
    return du.free / du.total


def preclean() -> None:
    """Best effort, before every batch session: undo anything a crashed session left running (shaper qdiscs, tcpdump,
    the collection Chrome). Each target is idempotent."""
    here = Path(__file__).parent
    for target in ("stop-capture", "disable", "kill_chrome"):
        subprocess.run(["make", "-s", target], cwd=here, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "pkill", "-INT", "-f", "[t]cpdump -i ifb"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


LAST_COVERAGE: dict[str, dict] = {}       # platform -> the latest coverage record (geometry_forced: Q am. 15 (3))


def coupled_geometry(p: str, geom: str | None) -> str | None:
    """Q am. 15 (3): the latest coverage target's coupled geometry when it has one (yt Q_1080p -> fullscreen), else
    the rotation's draw."""
    return (LAST_COVERAGE.get(p) or {}).get("geometry_forced") or geom


def session_matrix(p: str, summary_file: str = coverage_report.SUMMARY_FILE, excluded_geometries: tuple = ()) -> str | None:
    """The session's shaper matrix (MATRIX_RULE). The coverage record goes to data/_summaries/coverage_last.json and
    LVDC_COVERAGE_JSON for exp.py's meta.json; a failure of the rule falls back to MATRIX_IDS, recorded as such."""
    rec = {"platform": p, "rule": "fixed", "matrix": MATRIX_IDS.get(p)}
    if MATRIX_RULE == "coverage" and p in coverage_report.CLASSES:
        try:
            rec = (coverage_report.choose(_RUNS_DIR, p, COVERAGE_ALLOWED.get(p), excluded_geometries) if excluded_geometries
                   else coverage_report.choose(_RUNS_DIR, p, COVERAGE_ALLOWED.get(p)))
            print(f"  coverage : matrix {rec['matrix']} (target {(rec['target'] or {}).get('key')}, {rec['why']})")
        except Exception as e:
            rec["rule_error"] = repr(e)
            print(f"  coverage rule failed ({e!r}): matrix {rec['matrix']} (fixed)")
    LAST_COVERAGE[p] = rec
    path = _RUNS_DIR / summary_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=1, default=str))
    os.environ["LVDC_COVERAGE_JSON"] = str(path)
    return rec["matrix"]


FLAGS_FILE = "_summaries/flags.jsonl"


def report_flags(p: str, since: float, runs_dir: Path | None = None) -> list[dict]:
    """After a session (supervisor 2026-10-18: a collection fault is caught on the first session): every check.json of
    `p` written since `since` (accepted or _failed) with stream-regime flags gets a FLAG line and a flags.jsonl row;
    `new` lists the flags this platform had not raised before."""
    root = runs_dir or _RUNS_DIR
    path = root / FLAGS_FILE
    seen: set = set()
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("platform") == p:
                seen.update(r.get("flags") or [])
    rows = []
    found = []
    for c in list((root / p).glob("*/check.json")) + list((root / "_failed" / p).glob("*/check.json")):
        try:
            if c.stat().st_mtime >= since:
                found.append((c.stat().st_mtime, str(c), c, json.loads(c.read_text())))
        except Exception:
            continue
    for _, _, c, chk in sorted(found, key=lambda x: x[:2]):
        sus = chk.get("suspicious") or []
        if not sus:
            continue
        keys = [x.get("flag") for x in sus]
        row = {"t": time.time(), "platform": p, "session": c.parent.name, "verdict": chk.get("verdict"),
               "flags": keys, "new": [k for k in keys if k not in seen], "detail": sus}
        seen.update(keys)
        rows.append(row)
        print(f"  FLAG {p}/{c.parent.name}: {keys}" + (f" (NEW on {p}: {row['new']})" if row["new"] else "") + f" {sus}")
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=str) + "\n")
    return rows


def _summary() -> None:
    try:
        print(f"  Summary → {batch_summary.write(_RUNS_DIR)}")
    except Exception as e:
        print(f"  (daily summary failed: {e!r})")


def _pick(p: str, not_live: dict[str, float], now: float, rotate_geometry: bool, exclude_modes: tuple = (),
          root: Path | None = None) -> tuple:
    """The session's channel (yt), category (twitch) and geometry draw, and the skip records they leave (run_batch and,
    per slot, run_trio). root (the trio's data root): its skip records go there, and Addendum P's leave rule, single
    mode's list upkeep, does not run (every trio write stays under data/_trio)."""
    skip = {h for h, t in not_live.items() if now - t < NOT_LIVE_RETRY_S}
    out = root or _RUNS_DIR
    if p == "youtube" and root is None:
        for r in yt_channels.apply_leave_rule(_RUNS_DIR, now):
            print(f"  LIST: {r['handle']} ({r['type']}, {r['role']}) leaves the yt list ({r['evidence']}; Addendum P); "
                  f"commit it in yt_channels.py")
    yt_skips = yt_channels.not_live_skips(_RUNS_DIR, now) if p == "youtube" else {}
    for h, why in yt_skips.items():
        print(f"  skip {h}: {why}")
    channel = None
    if p == "youtube":
        # pre-session liveness probe (between sessions, outside capture; fail open; not a strike)
        channel, pre_skips = yt_channels.pick_live_channel(_RUNS_DIR, exclude=skip | set(yt_skips))
        if pre_skips:
            (out / "_summaries").mkdir(parents=True, exist_ok=True)
            with open(out / yt_channels.PRECHECK_SKIPS_FILE, "a") as fh:
                for s in pre_skips:
                    fh.write(json.dumps(s) + "\n")
                    print(f"  pre-check: {s['handle']} not live now, skipped (not a strike)")
    # addendum O: the twitch category rotation (a category whose directory gave no playable stream is skipped
    # NOT_LIVE_RETRY_S, like a yt channel)
    category, esports_skip = tw_categories.next_category(_RUNS_DIR, exclude=skip) if p == "twitch" else (None, None)
    if esports_skip:      # levi: no league channel live -> the next type; the skip goes to the daily summary
        (out / "_summaries").mkdir(parents=True, exist_ok=True)
        with open(out / "_summaries" / "tw_esports_skips.jsonl", "a") as fh:
            fh.write(json.dumps(esports_skip) + "\n")
        print(f"  esports unavailable (none of {esports_skip['checked']} live): drawing {category['name']}")
    geom = (geometry.next_geometry(_RUNS_DIR, p, exclude_modes) if exclude_modes else geometry.next_geometry(_RUNS_DIR, p)) \
        if rotate_geometry else None
    return channel, category, geom


def _account(p: str, rc: int, before: int, channel: dict | None, category: dict | None, now: float,
             paused: dict, fails: dict, frozen: dict, not_live: dict, runs_dir: Path | None = None) -> None:
    """A finished session's exit code into the batch state: login-wall pause, not-a-failure aborts, the not-live skip,
    the consecutive-failure pause (run_batch and, per slot, run_trio)."""
    if rc == EXIT_LOGIN_WALL:
        paused[p] = "login wall (fix the profile template, then rerun)"
    elif rc in NOT_A_FAILURE:
        frozen[p] += 1
        print(f"  {NOT_A_FAILURE[rc]} on {p} ({frozen[p]} such aborts in a row): not counted as a failure; "
              f"next session re-picks")
        if frozen[p] >= MAX_CONSECUTIVE_FROZEN:
            paused[p] = f"{frozen[p]} consecutive player_frozen / room_ended / player_dead aborts"
    elif rc != 0 and channel:
        not_live[channel["handle"]] = now
    elif rc != 0 and category:
        not_live[category.get("channel") or category["name"]] = now
    if rc not in NOT_A_FAILURE:
        frozen[p] = 0
        fails[p] = 0 if (accepted_sessions(p, runs_dir) if runs_dir else accepted_sessions(p)) > before else fails[p] + 1
    if fails[p] >= MAX_CONSECUTIVE_FAIL and p not in paused:
        paused[p] = f"{fails[p]} consecutive sessions without a PASS"
    if p in paused:
        print(f"  PAUSED {p}: {paused[p]}")


def run_batch(targets: dict[str, int], auto: bool, yt_matrix: str, rotate_geometry: bool = True) -> None:
    """Collect until every platform has its target of PASS sessions. Next platform = next_platform(): the one furthest
    behind its target (accepted / target; BATCH_ORDER breaks ties), at most MAX_CONSECUTIVE_SAME_PLATFORM in a row, so
    platforms advance together over the week. Nothing here
    changes what a session collects; it only sequences exp.py runs and recovers from failures."""
    paused: dict[str, str] = {}
    fails = {p: 0 for p in targets}
    frozen = {p: 0 for p in targets}
    not_live: dict[str, float] = {}
    recent = recent_platforms(_RUNS_DIR)
    while True:
        todo = [p for p in BATCH_ORDER if p in targets and p not in paused and accepted_sessions(p) < targets[p]]
        if not todo:
            break
        p = next_platform(todo, lambda q: accepted_sessions(q) / targets[q], recent)
        recent = (recent + [p])[-10:]
        fs = free_share()
        free_gb = shutil.disk_usage(_RUNS_DIR if _RUNS_DIR.exists() else _RUNS_DIR.parent).free / 1e9
        if free_gb < MIN_FREE_GB:
            print(f"\n  Data volume {free_gb:.0f} GB ({fs:.1%}) free < {MIN_FREE_GB} GB: stopping the batch (free space, then rerun).")
            _summary()
            sys.exit(EXIT_LOW_DISK)
        before = accepted_sessions(p)
        now = time.time()
        channel, category, geom = _pick(p, not_live, now, rotate_geometry)
        print(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M}] {p.upper()}: {before}/{targets[p]} accepted"
              + (f"; channel {channel['handle']} ({channel['type']}, {channel['role']})" if channel else "")
              + (f"; category {category['name']} ({category['type']}{', ' + category['channel'] if category.get('channel') else ''})" if category else "")
              + (f"; geometry {geom}" if geom else ""))
        preclean()
        # per-session preflight (supervisor 2026-10-18, after the 02:20 IPv6 incident): IPv6 re-applied and off,
        # tc + ifb present, data volume mounted above the floor, holds intact; refuse the session otherwise
        pf = preflight.run(_RUNS_DIR, MIN_FREE_GB)
        (_RUNS_DIR / "_summaries").mkdir(parents=True, exist_ok=True)
        pf_path = _RUNS_DIR / "_summaries" / "preflight_last.json"
        pf_path.write_text(json.dumps(pf, indent=1))
        os.environ["LVDC_PREFLIGHT_JSON"] = str(pf_path)   # exp.py records it in meta.json
        if not pf["ok"]:
            print(f"\n  PREFLIGHT FAILED ({', '.join(pf['problems'])}): not starting the session. "
                  f"{json.dumps({k: pf['checks'][k] for k in pf['problems']})}")
            _summary()
            sys.exit(EXIT_PREFLIGHT)
        mid = session_matrix(p)
        forced = coupled_geometry(p, geom) if rotate_geometry else geom
        if forced != geom:                                    # Q am. 15 (3): the target's coupled geometry
            print(f"  geometry {forced} (coupled to the coverage target; rotation drew {geom})")
            geom = forced
        t_session = time.time()
        try:
            rc = _run_collection(p, initial_state=most_needed_state(p, _RUNS_DIR, yt_matrix=mid, matrix_id=mid),
                                 episodes=EPISODES_V2.get(p, 5), auto=auto, yt_matrix=yt_matrix, channel=channel,
                                 geom=geom, tw_category=category, matrix=mid)
        except Exception as e:
            print(f"  exp.py could not run: {e!r}")
            rc = -1
        try:
            report_flags(p, t_session)
        except Exception as e:
            print(f"  (flag report failed: {e!r})")
        if rc == EXIT_STOPPED:
            print("  Session stopped by the operator (SIGINT / SIGTERM): stopping the batch.")
            _summary()
            sys.exit(EXIT_STOPPED)
        _account(p, rc, before, channel, category, now, paused, fails, frozen, not_live)
        _summary()
    print("\nBatch finished." + (f" Paused: {paused}" if paused else ""))
    for p in targets:
        print(f"  {p}: {accepted_sessions(p)}/{targets[p]} accepted")
    _summary()


# ---------------------------------------------------------------------------
# t53 --concurrent: the trio (design LVQoE concurrent_trio_design_peter_2026-10-18.md, A1 rolling slots)
# ---------------------------------------------------------------------------

TRIO_STAGGER_S = 30.0     # at least this long between two slot starts (Chrome launches and profile clones not at once)
TRIO_POLL_S = 2.0
# under the trio root (data/_trio, trio_slots.TRIO_DIR)
TRIO_DRAIN_FILE = "_summaries/trio_drain"   # touch it to drain: no refill, every slot ends; archived when used
TRIO_LOGS = "_summaries/trio_logs"          # each trio session's exp.py output (three would interleave on stdout)


def _spawn(cmd: list[str], env: dict, log: Path):
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as fh:
        return subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT)


def _make(args: list[str]) -> int:
    return subprocess.run(["make", "-s", *args], cwd=Path(__file__).parent).returncode


class _TrioStop(Exception):
    def __init__(self, rc: int):
        super().__init__(rc)
        self.rc = rc


def free_gb(path: Path) -> float:
    """The free space of path's volume in GB (the trio's low-disk stop; a function so tests set it, not the disk)."""
    return shutil.disk_usage(path).free / 1e9


def _trio_refill(p: str, T, targets: dict, started: dict, running: dict, paused: dict, not_live: dict,
                 rotate_geometry: bool, yt_matrix: str, capacity_kbit: float | None, run_id: str, summaries: Path,
                 spawn, floor: dict | None = None, stop_signal: list | None = None) -> bool:
    """Start the next session in p's slot (run_trio); False when its preflight failed (the trio drains)."""
    s, troot = T.SLOTS[p], _RUNS_DIR / T.TRIO_DIR
    gb = free_gb(_RUNS_DIR)
    if gb < MIN_FREE_GB:
        print(f"\n  Data volume {gb:.0f} GB free < {MIN_FREE_GB} GB: draining the trio.")
        raise _TrioStop(EXIT_LOW_DISK)
    floor = floor or T.trio_root_floor_gb(_RUNS_DIR, preflight.ROOT_MIN_FREE_GB)
    pf = {**preflight.run(_RUNS_DIR, MIN_FREE_GB, root_min_free_gb=floor["floor_gb"]), "trio_root_floor": floor}
    pf_path = summaries / f"preflight_last_{s.tag}.json"
    pf_path.write_text(json.dumps(pf, indent=1))
    if not pf["ok"]:
        print(f"\n  PREFLIGHT FAILED for the {p} slot ({', '.join(pf['problems'])}): draining the trio. "
              f"{json.dumps({k: pf['checks'][k] for k in pf['problems']})}")
        return False
    before, now = accepted_sessions(p, troot), time.time()
    # the pick and the coverage rule read single mode's data (steering only; trio's own counts are its root's); trio v1
    # draws no fullscreen geometry and a class needing one is unreachable (supervisor 2026-10-19)
    channel, category, geom = _pick(p, not_live, now, rotate_geometry, T.EXCLUDED_GEOMETRIES, troot)
    mid = session_matrix(p, f"{T.TRIO_DIR}/_summaries/coverage_last_{s.tag}.json", T.EXCLUDED_GEOMETRIES)
    forced = coupled_geometry(p, geom) if rotate_geometry else geom
    if forced and forced.split(":")[0] in T.EXCLUDED_GEOMETRIES:
        forced = geom
    if forced != geom:
        print(f"  geometry {forced} (coupled to the coverage target; rotation drew {geom})")
        geom = forced
    cmd = _collection_cmd(p, initial_state=most_needed_state(p, _RUNS_DIR, yt_matrix=mid, matrix_id=mid),
                          episodes=EPISODES_V2.get(p, 5), auto=True, yt_matrix=yt_matrix, channel=channel,
                          geom=geom, tw_category=category, matrix=mid)
    cmd += ["--slot", s.tag] + (["--link-capacity-kbit", str(capacity_kbit)] if capacity_kbit else [])
    # shots: CDP in the "cdp" layout (three windows on one screen), X11 crops in the "fb" layout (design §4)
    env = {**os.environ, "LVDC_TRIO_RUN": run_id, "LVDC_PREFLIGHT_JSON": str(pf_path),
           "LVQOE_SHOT_SOURCE": "x11" if os.environ.get("LVDC_TRIO_LAYOUT") == "fb" else "cdp"}
    if stop_signal:                     # a stop arrived during the pick: no new session
        print(f"  TRIO {p}: stop requested during the pick, no session started")
        return True
    started[p] += 1
    log = troot / TRIO_LOGS / run_id / f"{s.tag}_{started[p]}.log"
    print(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M}] TRIO {p} slot ({s.tag}) session {started[p]}: "
          f"{before}/{targets[p]} accepted"
          + (f"; channel {channel['handle']}" if channel else "")
          + (f"; category {category['name']}" if category else "")
          + (f"; geometry {geom}" if geom else "") + f"\n  Running: {' '.join(cmd)}\n  log: {log}")
    running[p] = {"proc": spawn(cmd, env, log), "before": before, "channel": channel, "category": category,
                  "now": now, "t": time.time(), "log": str(log)}
    return True


def _xrandr(fb: str | None):
    """`xrandr --fb <fb>` (None: no change); the X screen's size after it, (w, h)."""
    if fb:
        subprocess.run(["xrandr", "--fb", fb], capture_output=True, text=True, timeout=15)
    out = subprocess.run(["xrandr", "--current"], capture_output=True, text=True, timeout=15).stdout
    import re
    m = re.search(r"current (\d+) x (\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def run_trio(targets: dict[str, int], yt_matrix: str, rotate_geometry: bool = True, iface: str = "wlp0s20f3",
             capacity_kbit: float | None = None, max_per_slot: int | None = None,
             stagger_s: float = TRIO_STAGGER_S, poll_s: float = TRIO_POLL_S, layout: str = "cdp",
             spawn=_spawn, apply=None, make=_make, sleep=time.sleep, xrandr=None) -> int:
    """One slot per platform (trio_slots.SLOTS), each refilled when its session ends (A1), until every platform has its
    target of PASS trio sessions (counted in the trio's own data root, data/_trio/<platform>: single mode never sees
    them), or `max_per_slot` sessions started per slot (the pilot: 1). Trio v1 draws no fullscreen geometry, and a
    coverage class that needs one is unreachable (recorded). Per slot exactly as run_batch
    per session: the pick (_pick), the per-session preflight (rootfs floor raised by A2), the coverage matrix and its
    coupled geometry, the accounting (_account), the flag report; each slot's coverage / preflight record is its own
    file. Started in a pause (no session running): single mode's preclean once, then the shared tree, the unmarked
    remainder's capture, then the slots; no orchestrator-wide preclean while a slot runs (a slot cleans only its own
    pieces, exp.py slot_up). Drain (TRIO_DRAIN_FILE, or SIGINT / SIGTERM, which are also passed to every running
    session) stops refilling; once every slot has ended the default capture is merged and the shared tree removed.
    layout: "cdp" (pilot-0: windows at the origin, CDP shots) or "fb" (design §4: the X framebuffer enlarged to three
    monitor widths for the run, restored at its end; slot k's window at k x the width; X11 shots).
    Returns the exit code: 0, or EXIT_STOPPED / EXIT_LOW_DISK / EXIT_PREFLIGHT when that ended it."""
    import signal
    import trio_slots as T
    apply = apply or T.apply
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if not _RUNS_DIR.is_dir():           # never create data/ on the root filesystem (a missing data link)
        print(f"\n  TRIO: the data dir {_RUNS_DIR} does not exist: not starting the trio.")
        return EXIT_PREFLIGHT
    troot = _RUNS_DIR / T.TRIO_DIR                          # trio sessions' own data root (never pooled silently)
    summaries = troot / "_summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    slots_log, drain_path = troot / T.SLOTS_LOG, troot / TRIO_DRAIN_FILE
    paused: dict[str, str] = {}
    fails = {p: 0 for p in targets}
    frozen = {p: 0 for p in targets}
    not_live: dict[str, float] = {}
    started = {p: 0 for p in targets}
    running: dict[str, dict] = {}
    end_rc, last_start = 0, 0.0
    stop_signal: list[int] = []

    def _on_signal(sig, _frame):
        stop_signal.append(sig)
        for r in running.values():
            try:
                r["proc"].send_signal(signal.SIGTERM)
            except Exception:
                pass

    def _consume_drain(why: str) -> None:      # archived, so a later trio is not drained by it
        if drain_path.exists():
            drain_path.rename(drain_path.with_name(f"{drain_path.name}.{why}_{run_id}"))

    _consume_drain("stale")
    repo = Path(__file__).resolve().parent.parent
    tpl = Path(os.environ.get("LVDC_PROFILE_TEMPLATES", str(repo / "profiles")))
    pre = T.trio_preflight([p for p in targets if p in T.SLOTS], repo, tpl, _RUNS_DIR)
    (summaries / "trio_preflight_last.json").write_text(json.dumps(pre, indent=1))
    if not pre["ok"]:                    # nothing touched: no tree, no capture, no session
        print(f"\n  TRIO PREFLIGHT FAILED ({', '.join(pre['problems'])}): not starting the trio.")
        return EXIT_PREFLIGHT
    floor = T.trio_root_floor_gb(_RUNS_DIR, preflight.ROOT_MIN_FREE_GB)   # once per run (it stats every pcap)
    old = {s: signal.signal(s, _on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    preclean()
    for s in T.SLOTS.values():
        apply(T.slot_down_cmds(s, iface, T.nft_handle_of(s)), check=False)
    apply(T.shared_down_cmds(iface), check=False)
    xrandr = xrandr or _xrandr
    screen = fb = dstage = ddir = ct_before = None
    try:
        apply(T.shared_up_cmds(iface))
        os.environ["LVDC_TRIO_LAYOUT"] = layout
        if layout == "fb":               # design §4: room for three non-overlapping windows beside the monitor
            screen = xrandr(None)
            fb = xrandr(f"{len(T.SLOTS) * screen[0]}x{screen[1]}") if screen else None
            os.environ["LVDC_TRIO_SCREEN"] = f"{screen[0]}x{screen[1]}" if screen else ""
            if not screen or not fb or fb[0] < len(T.SLOTS) * screen[0]:
                print(f"  TRIO: the framebuffer did not grow (screen {screen}, now {fb}): no session starts")
                end_rc = EXIT_PREFLIGHT
        # every name the run's sessions use is then asked upstream within the run, so it reaches a DNS capture (v3 §8)
        flush = apply(["resolvectl flush-caches"], check=False)
        # an idle QUIC flow keeps its conntrack entry, so its mark, through the session (levi 1b); restored at the end
        ct_before = T.conntrack_timeouts()
        ct_set = apply([f"sysctl -w net.netfilter.nf_conntrack_udp_timeout_stream={T.TRIO_UDP_STREAM_TIMEOUT_S}"],
                       check=False)
        dstage = T.STAGING / f"default_{run_id}"
        dstage.mkdir(parents=True, exist_ok=True)
        ddir = troot / T.DEFAULT_CAPTURE / run_id
        make(["start-capture", f"SESSION_DIR={ddir}"] + T.default_capture_vars(dstage))
        T.slots_log_append(slots_log, {"event": "trio_up", "run": run_id, "t": time.time(), "iface": iface,
                                       "capacity_kbit": capacity_kbit, "targets": targets, "max_per_slot": max_per_slot,
                                       "conntrack_timeouts_before": ct_before, "conntrack_timeouts": T.conntrack_timeouts(),
                                       "conntrack_set": ct_set[0], "resolver_cache_flushed": flush[0],
                                       "layout": layout, "screen": screen, "framebuffer": fb})
        print(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M}] TRIO {run_id} up: {targets}, max per slot {max_per_slot}, "
              f"layout {layout}")
        while True:
            for p, r in list(running.items()):
                rc = r["proc"].poll()
                if rc is None:
                    continue
                del running[p]
                T.slots_log_append(slots_log, {"event": "exit", "slot": T.SLOTS[p].tag, "rc": rc, "t": time.time()})
                print(f"[{dt.datetime.now():%H:%M}] TRIO {p}: exp.py rc {rc} (log {r['log']})")
                try:
                    report_flags(p, r["t"], troot)
                except Exception as e:
                    print(f"  (flag report failed: {e!r})")
                if rc == EXIT_STOPPED:
                    print(f"  {p}: session stopped (SIGINT / SIGTERM): draining the trio")
                    end_rc = end_rc or EXIT_STOPPED
                else:
                    _account(p, rc, r["before"], r["channel"], r["category"], r["now"], paused, fails, frozen, not_live,
                             troot)
            draining = bool(stop_signal) or end_rc != 0 or drain_path.exists()
            todo = [p for p in BATCH_ORDER if p in targets and p in T.SLOTS and p not in paused
                    and (max_per_slot is None or started[p] < max_per_slot) and accepted_sessions(p, troot) < targets[p]]
            if not running and (draining or not todo):
                break
            for p in ([] if draining else todo):
                if p in running or time.time() - last_start < stagger_s:
                    continue
                try:
                    if not _trio_refill(p, T, targets, started, running, paused, not_live, rotate_geometry, yt_matrix,
                                        capacity_kbit, run_id, summaries, spawn, floor, stop_signal):
                        end_rc = end_rc or EXIT_PREFLIGHT
                        break
                except _TrioStop as e:
                    end_rc = end_rc or e.rc
                    break
                except Exception as e:      # never a crash under running siblings: drain instead
                    print(f"  TRIO {p} slot could not start a session ({e!r}): draining the trio")
                    end_rc = end_rc or 1
                    break
                last_start = time.time()
            sleep(poll_s)
    finally:
        for p, r in running.items():        # only after an unexpected error: every session finalises before the teardown
            print(f"  TRIO: waiting for the {p} session to finalise before the teardown")
            try:
                r["proc"].send_signal(signal.SIGTERM)
                r["proc"].wait(timeout=900)
            except Exception as e:
                print(f"  ({p}: {e!r})")
        if ddir is not None:
            make(["stop-capture"] + T.default_capture_vars(dstage))
            make(["merge-capture", f"SESSION_DIR={ddir}"] + T.default_capture_vars(dstage))
        down = apply(T.shared_down_cmds(iface), check=False)
        ct_restored = None
        if ct_before and ct_before.get("nf_conntrack_udp_timeout_stream"):
            ct_restored = apply([f"sysctl -w net.netfilter.nf_conntrack_udp_timeout_stream="
                                 f"{ct_before['nf_conntrack_udp_timeout_stream']}"], check=False)[0]
        restored = xrandr(f"{screen[0]}x{screen[1]}") if screen else None
        T.slots_log_append(slots_log, {"event": "trio_down", "run": run_id, "t": time.time(), "end_rc": end_rc,
                                       "signal": stop_signal[:1], "teardown_failed": [x["cmd"] for x in down if x["rc"]],
                                       "framebuffer_restored": restored, "conntrack_restored": ct_restored})
        _consume_drain("done")
        for s, h in old.items():
            signal.signal(s, h)
    end_rc = end_rc or (EXIT_STOPPED if stop_signal else 0)
    print(f"\nTRIO {run_id} finished (rc {end_rc})." + (f" Paused: {paused}" if paused else ""))
    for p in targets:
        print(f"  {p}: {accepted_sessions(p, troot)}/{targets[p]} trio sessions accepted, {started[p]} started")
    return end_rc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Orchestrate QoE data collection with automatic balance targeting"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--batch",
        default=None,
        help="Collection v2 batch, e.g. 'youtube=30,twitch=15,tiktok=15': PASS-session targets per platform; resumes "
             "from what is on disk; free-space stop, failure recovery, daily summary (data/_summaries/).",
    )
    group.add_argument(
        "--platform",
        choices=list(PLATFORM_TRANSITIONS),
        help="Collect for a single platform.",
    )
    group.add_argument(
        "--all-platforms",
        action="store_true",
        help="Collect for all platforms in round-robin order.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of collection sessions per platform (default: 1).",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        default=None,
        help=(
            "State transitions (episodes) per session; each episode is ~60 s. "
            "Default: the collection v2 count per platform (EPISODES_V2: yt 21, tt 23, twitch 23). "
            "Overriding it is a deviation from the prereg."
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="exp.py --auto: navigate to the stream and handle ads (no operator prompt). Required for v2 batches.",
    )
    parser.add_argument("--tt-matrix", choices=["TT-M", "current"], default="TT-M",
                        help="tt shaper config (prereg addendum M; default TT-M).")
    parser.add_argument("--tw-matrix", choices=["TW-M", "TW-L", "current"], default="TW-M",
                        help="twitch shaper config (prereg addendum M; default TW-M; TW-L = Addendum Q amendment 3).")
    parser.add_argument("--tt-room-rotation", type=float, default=None, metavar="SECONDS",
                        help="addendum M's pre-declared tt fallback: a new room every SECONDS (300) within each tt "
                             "session. Off unless the checkpoint QA triggers it.")
    parser.add_argument(
        "--dpms-off",
        action="store_true",
        help="exp.py --dpms-off for every session: `xset dpms force off` after the browser start (opt-in).",
    )
    parser.add_argument(
        "--entry-quality",
        action="store_true",
        help="exp.py --entry-quality for every yt / twitch session (Q am. 1 / am. 10; default off until its tests, the "
             "laptop live check and the 3-per-platform chaos pilot pass).",
    )
    parser.add_argument(
        "--fixed-geometry",
        action="store_true",
        help="--batch: keep each platform's pinned player size instead of rotating geometry.py's list (rushi t37: rotate).",
    )
    parser.add_argument(
        "--yt-matrix",
        choices=["A", "B", "current"],
        default="A",
        help="yt shaper config (t38; default A). The yt balance target is this matrix's stationary distribution.",
    )
    parser.add_argument("--matrix-rule", choices=["coverage", "fixed"], default="coverage",
                        help="--batch: the shaper matrix per session from the coverage snapshot (prereg Addendum Q; "
                             "default) or fixed (--yt-matrix / --tt-matrix / --tw-matrix).")
    parser.add_argument("--coverage-allowed", default=None, metavar="P=ID|ID,...",
                        help="--matrix-rule coverage: the allowed matrices per platform, e.g. 'tiktok=TT-M|current' "
                             "(default: every defined matrix).")
    parser.add_argument("--concurrent", action="store_true",
                        help="--batch as a trio (t53; design concurrent_trio_design_peter_2026-10-18): one rolling slot "
                             "per platform (yt / twitch / tt) at once, each in its own cgroup, marks, IFBs, Chrome; "
                             "start it in a pause. Off: single mode, unchanged.")
    parser.add_argument("--link-capacity-kbit", type=float, default=None,
                        help="--concurrent: the measured multi-stream goodput (the gate 1 dry run); each session records "
                             "it and flags seconds whose summed slot rates exceed it (L-A2).")
    parser.add_argument("--trio-layout", choices=["cdp", "fb"], default="cdp",
                        help="--concurrent: cdp = windows at the origin, CDP shots (pilot-0); fb = the X framebuffer "
                             "enlarged to three monitor widths, one window per width, X11 shots (design §4; pilot-1).")
    parser.add_argument("--max-per-slot", type=int, default=None,
                        help="--concurrent: stop refilling a slot after this many sessions (the pilot: 1).")
    parser.add_argument("--iface", default=os.environ.get("LVDC_IFACE", "wlp0s20f3"),
                        help="--concurrent: the collection interface (the Makefile's IFACE).")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print balance report without collecting.",
    )
    args = parser.parse_args()

    DPMS_OFF = args.dpms_off
    ENTRY_QUALITY = args.entry_quality
    MATRIX_IDS = {"youtube": args.yt_matrix, "tiktok": args.tt_matrix, "twitch": args.tw_matrix}
    TT_ROOM_ROTATION_S = args.tt_room_rotation
    MATRIX_RULE = args.matrix_rule
    if args.coverage_allowed:
        COVERAGE_ALLOWED = {k.strip(): v.split("|") for k, v in (x.split("=") for x in args.coverage_allowed.split(","))}
        from utils.shaping import PLATFORM_MATRICES
        bad = {k: v for k, v in COVERAGE_ALLOWED.items() if k not in PLATFORM_MATRICES or set(v) - set(PLATFORM_MATRICES[k])}
        if bad:
            sys.exit(f"unknown platform / matrix in --coverage-allowed: {bad}")
    if args.concurrent and not args.batch:
        sys.exit("--concurrent needs --batch")
    if args.batch:
        targets = {k.strip(): int(v) for k, v in (x.split("=") for x in args.batch.split(","))}
        unknown = [k for k in targets if k not in PLATFORM_TRANSITIONS]
        if unknown:
            sys.exit(f"unknown platform(s) in --batch: {unknown}")
        if args.concurrent:
            sys.exit(run_trio(targets, yt_matrix=args.yt_matrix, rotate_geometry=not args.fixed_geometry,
                              iface=args.iface, capacity_kbit=args.link_capacity_kbit, max_per_slot=args.max_per_slot,
                              layout=args.trio_layout))
        run_batch(targets, auto=True, yt_matrix=args.yt_matrix, rotate_geometry=not args.fixed_geometry)
        sys.exit(0)

    platforms = list(PLATFORM_TRANSITIONS) if args.all_platforms else [args.platform]

    if args.report_only:
        for p in platforms:
            balance_report(p, _RUNS_DIR, yt_matrix=args.yt_matrix, matrix_id=MATRIX_IDS.get(p))
        sys.exit(0)

    for p in platforms:
        eps = args.episodes if args.episodes is not None else EPISODES_V2.get(p, 5)
        _collect_platform(p, runs=args.runs, episodes_per_run=eps, auto=args.auto, yt_matrix=args.yt_matrix)
