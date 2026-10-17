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
import geometry
import tw_categories
import yt_channels

_RUNS_DIR = Path(__file__).parent.parent / "data"

EXIT_LOGIN_WALL = 3   # exp.py's exit code for a session aborted at a login wall (t37)
EXIT_STOPPED = 130    # exp.py's exit code when the operator stopped the session
EXIT_PLAYER_FROZEN = 5   # exp.py's exit code when the watchdog aborted a frozen player (addendum N)
EXIT_ROOM_ENDED = 6      # exp.py's exit code when a tt room died mid-session (room_ended)
# aborts that are not failures of the platform: they do not count toward MAX_CONSECUTIVE_FAIL
NOT_A_FAILURE = {EXIT_PLAYER_FROZEN: "player_frozen", EXIT_ROOM_ENDED: "room_ended"}

EXIT_LOW_DISK = 4     # orchestrate.py's own exit code when the data volume falls below MIN_FREE_SHARE free

# collection v2 (LVQoE prereg t36 §1): episodes per session are fixed per platform, not left to the operator
EPISODES_V2 = {"youtube": 21, "tiktok": 23, "twitch": 23}

# --batch (t37, week-long run): stop before a session when the data volume has less than this share free; pause a
# platform after this many consecutive sessions without a PASS; a yt channel that failed to start is skipped this long
MIN_FREE_SHARE = 0.10
MAX_CONSECUTIVE_FAIL = 5
# NOT_A_FAILURE aborts (player_frozen, room_ended) do not count toward MAX_CONSECUTIVE_FAIL; a separate cap stops a
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


DPMS_OFF = False   # --dpms-off: pass exp.py --dpms-off (chaos: re-blank the monitor after each session's window resize)
# shaper matrix per platform (yt t38; tt / twitch prereg addendum M) and the tt room-rotation fallback (seconds or None)
MATRIX_IDS: dict[str, str] = {"youtube": "A", "tiktok": "TT-M", "twitch": "TW-M"}
TT_ROOM_ROTATION_S: float | None = None


def _run_collection(platform: str, initial_state: str | None, episodes: int, auto: bool = False,
                    yt_matrix: str | None = None, channel: dict | None = None, geom: str | None = None,
                    tw_category: dict | None = None) -> int:
    cmd = [
        sys.executable, "exp.py",
        "--platform", platform,
        "--episodes", str(episodes),
    ]
    if initial_state:
        cmd += ["--initial-state", initial_state]
    if auto:
        cmd += ["--auto"]
    if platform == "youtube" and yt_matrix:
        cmd += ["--yt-matrix", yt_matrix]
    if platform == "tiktok":
        cmd += ["--tt-matrix", MATRIX_IDS["tiktok"]]
        if TT_ROOM_ROTATION_S:
            cmd += ["--tt-room-rotation", str(TT_ROOM_ROTATION_S)]
    if platform == "twitch":
        cmd += ["--tw-matrix", MATRIX_IDS["twitch"]]
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
    print(f"\n  Running: {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def _collect_platform(platform: str, runs: int, episodes_per_run: int, auto: bool = False,
                      yt_matrix: str | None = None) -> None:
    not_live: set[str] = set()   # yt channels whose session failed to start this invocation (tried once each)
    for i in range(runs):
        print(f"\n[{platform.upper()} — run {i+1}/{runs}]")
        target_state = most_needed_state(platform, _RUNS_DIR, yt_matrix=yt_matrix, matrix_id=MATRIX_IDS.get(platform))
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


def accepted_sessions(platform: str) -> int:
    """PASS sessions on disk (data/<platform>/*/check.json); a FAIL is archived to data/_failed and not counted. The
    batch recounts these at every start, so a restart resumes where it stopped."""
    n = 0
    for c in (_RUNS_DIR / platform).glob("*/check.json"):
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


def _summary() -> None:
    try:
        print(f"  Summary → {batch_summary.write(_RUNS_DIR)}")
    except Exception as e:
        print(f"  (daily summary failed: {e!r})")


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
        if fs < MIN_FREE_SHARE:
            print(f"\n  Data volume {fs:.1%} free < {MIN_FREE_SHARE:.0%}: stopping the batch (free space, then rerun).")
            _summary()
            sys.exit(EXIT_LOW_DISK)
        before = accepted_sessions(p)
        now = time.time()
        skip = {h for h, t in not_live.items() if now - t < NOT_LIVE_RETRY_S}
        yt_skips = yt_channels.not_live_skips(_RUNS_DIR, now) if p == "youtube" else {}
        for h, why in yt_skips.items():
            print(f"  skip {h}: {why}")
        channel = None
        if p == "youtube":
            # pre-session liveness probe (between sessions, outside capture; fail open; not a strike)
            channel, pre_skips = yt_channels.pick_live_channel(_RUNS_DIR, exclude=skip | set(yt_skips))
            if pre_skips:
                (_RUNS_DIR / "_summaries").mkdir(parents=True, exist_ok=True)
                with open(_RUNS_DIR / "_summaries" / "yt_precheck_skips.jsonl", "a") as fh:
                    for s in pre_skips:
                        fh.write(json.dumps(s) + "\n")
                        print(f"  pre-check: {s['handle']} not live now, skipped (not a strike)")
        # addendum O: the twitch category rotation (a category whose directory gave no playable stream is skipped
        # NOT_LIVE_RETRY_S, like a yt channel)
        category, esports_skip = tw_categories.next_category(_RUNS_DIR, exclude=skip) if p == "twitch" else (None, None)
        if esports_skip:      # levi: no league channel live -> the next type; the skip goes to the daily summary
            (_RUNS_DIR / "_summaries").mkdir(parents=True, exist_ok=True)
            with open(_RUNS_DIR / "_summaries" / "tw_esports_skips.jsonl", "a") as fh:
                fh.write(json.dumps(esports_skip) + "\n")
            print(f"  esports unavailable (none of {esports_skip['checked']} live): drawing {category['name']}")
        geom = geometry.next_geometry(_RUNS_DIR, p) if rotate_geometry else None
        print(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M}] {p.upper()}: {before}/{targets[p]} accepted"
              + (f"; channel {channel['handle']} ({channel['type']}, {channel['role']})" if channel else "")
              + (f"; category {category['name']} ({category['type']}{', ' + category['channel'] if category.get('channel') else ''})" if category else "")
              + (f"; geometry {geom}" if geom else ""))
        preclean()
        try:
            rc = _run_collection(p, initial_state=most_needed_state(p, _RUNS_DIR, yt_matrix=yt_matrix, matrix_id=MATRIX_IDS.get(p)),
                                 episodes=EPISODES_V2.get(p, 5), auto=auto, yt_matrix=yt_matrix, channel=channel,
                                 geom=geom, tw_category=category)
        except Exception as e:
            print(f"  exp.py could not run: {e!r}")
            rc = -1
        if rc == EXIT_STOPPED:
            print("  Session stopped by the operator (SIGINT / SIGTERM): stopping the batch.")
            _summary()
            sys.exit(EXIT_STOPPED)
        if rc == EXIT_LOGIN_WALL:
            paused[p] = "login wall (fix the profile template, then rerun)"
        elif rc in NOT_A_FAILURE:
            frozen[p] += 1
            print(f"  {NOT_A_FAILURE[rc]} on {p} ({frozen[p]} such aborts in a row): not counted as a failure; "
                  f"next session re-picks")
            if frozen[p] >= MAX_CONSECUTIVE_FROZEN:
                paused[p] = f"{frozen[p]} consecutive player_frozen / room_ended aborts"
        elif rc != 0 and channel:
            not_live[channel["handle"]] = now
        elif rc != 0 and category:
            not_live[category.get("channel") or category["name"]] = now
        if rc not in NOT_A_FAILURE:
            frozen[p] = 0
            fails[p] = 0 if accepted_sessions(p) > before else fails[p] + 1
        if fails[p] >= MAX_CONSECUTIVE_FAIL and p not in paused:
            paused[p] = f"{fails[p]} consecutive sessions without a PASS"
        if p in paused:
            print(f"  PAUSED {p}: {paused[p]}")
        _summary()
    print("\nBatch finished." + (f" Paused: {paused}" if paused else ""))
    for p in targets:
        print(f"  {p}: {accepted_sessions(p)}/{targets[p]} accepted")
    _summary()


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
    parser.add_argument("--tw-matrix", choices=["TW-M", "current"], default="TW-M",
                        help="twitch shaper config (prereg addendum M; default TW-M).")
    parser.add_argument("--tt-room-rotation", type=float, default=None, metavar="SECONDS",
                        help="addendum M's pre-declared tt fallback: a new room every SECONDS (300) within each tt "
                             "session. Off unless the checkpoint QA triggers it.")
    parser.add_argument(
        "--dpms-off",
        action="store_true",
        help="exp.py --dpms-off for every session: `xset dpms force off` after the browser start (opt-in).",
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
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print balance report without collecting.",
    )
    args = parser.parse_args()

    DPMS_OFF = args.dpms_off
    MATRIX_IDS = {"youtube": args.yt_matrix, "tiktok": args.tt_matrix, "twitch": args.tw_matrix}
    TT_ROOM_ROTATION_S = args.tt_room_rotation
    if args.batch:
        targets = {k.strip(): int(v) for k, v in (x.split("=") for x in args.batch.split(","))}
        unknown = [k for k in targets if k not in PLATFORM_TRANSITIONS]
        if unknown:
            sys.exit(f"unknown platform(s) in --batch: {unknown}")
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
