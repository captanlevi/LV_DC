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

    # Parse only (ops dry_window.sh): would this command line be refused? One JSON line, exit 0 / 2; see check_args
    python orchestrate.py --check-args --batch youtube=1 --concurrent ...     (--check-args anywhere in the line)
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
import tw_latency
import yt_channels

from data_root import DATA_ROOT as _RUNS_DIR  # noqa: E402  (one setting: data_root.py)

EXIT_LOGIN_WALL = 3   # exp.py's exit code for a session aborted at a login wall (t37)
EXIT_STOPPED = 130    # exp.py's exit code when the operator stopped the session
EXIT_PLAYER_FROZEN = 5   # exp.py's exit code when the watchdog aborted a frozen player (addendum N)
EXIT_ROOM_ENDED = 6      # exp.py's exit code when a tt room died mid-session (room_ended)
EXIT_PLAYER_DEAD = 7     # exp.py's exit code when the tt player sat at readyState 0 with no media (player_dead)
# aborts that are not failures of the platform: they do not count toward MAX_CONSECUTIVE_FAIL
EXIT_STREAM_ENDED = 12   # exp.py's exit code when a yt / twitch tab left its live stream (stream_ended; bundle item 3)
EXIT_PLAYER_ERROR_HELD = 13   # exp.py's code for a yt / twitch player error begun in the low-state hold (levi 02:57)
# levi 02:57: a player error our low-state shaping explains is re-picked like tt's player_dead (no failure streak, no
# not-live mark); one outside the hold (state >= 5, or unshaped past startup) still exits 1: a failure, channel marked
NOT_A_FAILURE = {EXIT_PLAYER_FROZEN: "player_frozen", EXIT_ROOM_ENDED: "room_ended", EXIT_PLAYER_DEAD: "player_dead",
                 EXIT_STREAM_ENDED: "stream_ended", EXIT_PLAYER_ERROR_HELD: "player_error (low-state hold)"}

TRIO_SHOT_SOURCE = {"cdp": "cdp", "fb": "x11", "composite": "composite"}   # trio layout -> LVQOE_SHOT_SOURCE
EXIT_LOW_DISK = 4     # orchestrate.py's own exit code when the data volume has less than MIN_FREE_GB free
EXIT_OFFLOADS = 9    # orchestrate.py's own exit code when the capture offloads cannot be switched off (t55)
EXIT_PREFLIGHT = 8    # orchestrate.py's own exit code when the per-session preflight fails (preflight.py)
EXIT_WINDOW_READ = 10   # orchestrate.py's own exit code: more than one slot paused in a window, a stop-and-read (levi 00138)

# collection v2 (LVQoE prereg t36 §1): episodes per session are fixed per platform, not left to the operator
EPISODES_V2 = {"youtube": 21, "tiktok": 23, "twitch": 23}
from utils.shaping import session_episodes, schedule_of, draw_session_minutes, draw_fast_episode_s  # noqa: E402

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
                    tw_category: dict | None = None, matrix: str | None = None,
                    initial_state_source: str | None = None, tt_room: str | None = None) -> list[str]:
    cmd = [
        sys.executable, "exp.py",
        "--platform", platform,
        "--episodes", str(episodes),
    ]
    if initial_state:
        cmd += ["--initial-state", initial_state]
        if initial_state_source:            # t65: the rule that chose it, recorded as steering.start_overridden_by
            cmd += ["--initial-state-source", initial_state_source]
    if auto:
        cmd += ["--auto"]
    if platform == "youtube" and (matrix or yt_matrix):
        cmd += ["--yt-matrix", matrix or yt_matrix]
    if platform == "tiktok":
        cmd += ["--tt-matrix", matrix or MATRIX_IDS["tiktok"]]
        if tt_room:                     # v3 W-A: a pinned room is opened directly and never rotated away
            cmd += ["--channel-url", tt_room]
        elif TT_ROOM_ROTATION_S:
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
    if geom:                            # t67 B1: launched by its instruction, recorded (and steered) by its realised name
        cmd += ["--geometry", geometry.instruction_of(platform, geom), "--geometry-name", geom]
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
        rc = _run_collection(platform, initial_state=target_state, initial_state_source="balance",
                             episodes=episodes_per_run, auto=auto,
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


def accepted_sessions(platform: str, runs_dir: Path | None = None, select=None) -> int:
    """PASS sessions on disk (data/<platform>/*/check.json); a FAIL is archived to data/_failed and not counted. The
    batch recounts these at every start, so a restart resumes where it stopped. runs_dir: the trio's own root; select:
    meta.json -> bool, the sessions that count (trio_select(): a counted run's sessions of record, t58 L-3 / L-5)."""
    n = 0
    for c in ((runs_dir or _RUNS_DIR) / platform).glob("*/check.json"):
        try:
            if json.loads(c.read_text()).get("verdict") != "PASS":
                continue
            n += select is None or bool(select(json.loads((c.parent / "meta.json").read_text())))
        except Exception:
            pass
    return n


def _slot_session(slots_log: Path, tag: str, run_id: str) -> str | None:
    """The session the slot's latest start in this run named (exp.py's slots-log start event; t58 N-4)."""
    name = None
    try:
        lines = Path(slots_log).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "start" and e.get("slot") == tag and e.get("run") == run_id:
            name = e.get("session")
    return name


def _session_passed(troot: Path, p: str, name: str | None, select=None) -> bool:
    """The session counts as accepted (accepted_sessions' rule): PASS, under troot/<p> (not _failed), selected."""
    if not name:
        return False
    d = Path(troot) / p / name
    try:
        if json.loads((d / "check.json").read_text()).get("verdict") != "PASS":
            return False
        return select is None or bool(select(json.loads((d / "meta.json").read_text())))
    except (OSError, ValueError):
        return False


def _qa_fail_cause(root: Path, p: str, name: str | None = None, since: float | None = None) -> str | None:
    """check.json qa_fail_cause (levi 87562afd #12) of the finished session: by name under root/<p> or, moved by
    --move-failed, root/_failed/<p>; without a name (run_batch), the newest check.json written since `since`. None when
    absent or unreadable (the session then counts as today)."""
    if name:
        cands = [Path(root) / p / name / "check.json", Path(root) / "_failed" / p / name / "check.json"]
    elif since is not None:
        try:
            cands = sorted((c for c in list((Path(root) / p).glob("*/check.json")) +
                            list((Path(root) / "_failed" / p).glob("*/check.json")) if c.stat().st_mtime >= since),
                           key=lambda c: c.stat().st_mtime, reverse=True)[:1]
        except OSError:
            return None
    else:
        return None
    for c in cands:
        try:
            return json.loads(c.read_text()).get("qa_fail_cause")
        except (OSError, ValueError):
            continue
    return None


TRIO_RUN_ID: str | None = None      # run_trio's run id (decode_stress: its targets count this run's sessions only)


def trio_select():
    """t58 L-3 / L-5 (levi; brad's audit item 6): one membership rule for the trio's targets, its failure count, the
    rotation hygiene (not_usable) and the steering: a counted run (--trio-record) counts its sessions of record only
    (record.is_record: the flag and the current ERA); a decode_stress run (--trio-stress) counts its own run's sessions
    only (meta.json concurrency.trio_run; shagun 2026-10-19: earlier stress sessions in data/_trio_stress met the
    targets and the N = 5 run started none); a pilot run counts every session in the trio root, as before."""
    import record
    if TRIO_RECORD:
        return record.is_record
    if TRIO_STRESS and TRIO_RUN_ID:
        run = TRIO_RUN_ID
        return lambda meta: ((meta or {}).get("concurrency") or {}).get("trio_run") == run
    return None


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
# t53 round pins (levi 00261: allowed steering if recorded; default off, trio only): platform -> {category, channel, state,
# geometry, reason}; the slot's pick is overridden where given and the session's meta.json records pick_pinned
TRIO_PINS: dict[str, dict] = {}
REPLAY: dict | None = None   # v3 W-A: --replay-schedule {seed, step_s}, passed to every trio session
TRIO_RECORD = False      # --trio-record: this run's sessions are of record (counted; record.ERA), steering counts only those
# t53 (a), default off: fullscreen is in the yt slots' geometry support (restart §7: the per-session draw; before it, the
# coverage target's coupling); the slot's process then runs single mode's fullscreen path with a focus keeper. Off: the
# draw's support excludes it (EXCLUDED_GEOMETRIES) and yt Q_1080p is unreachable (coverage.geometry_excluded)
TRIO_YT_FULLSCREEN = False
# prereg d32a48ce (label diversity at the 10-22 tip, item 2): --trio-steer hands each trio session the deficit table's
# path (LVDC_STEER_TABLE = <trio root>/_summaries/coverage_joint.json); exp.py draws the dwell weights at session start
TRIO_STEER = False
# levi 20:57 / 20:58 (tt failed the table's direction criterion): --trio-steer-platforms names the platforms steered; an
# unlisted one gets the base matrix, the base start rule, no TARGET_MINUTES, and meta.json steering_fallback "platform not
# steered". None (no list) = every platform, as signed at 057760cd.
TRIO_STEER_PLATFORMS: set[str] | None = None
STEER_SKIPPED = "platform not steered"
# restart §7 / §8 (levi's prereg 2026-10-21): each trio session's draws (geometry, twitch low latency) are seeded by
# f"{SESSION_DRAW_SEED}:{run id}:{slot tag}:{the slot's session number}"; each draw salts it with its own name
SESSION_DRAW_SEED = 20261021
# t56 decode_stress (--trio-stress): the run's sessions go under data/_trio_stress (LVDC_TRIO_DIR), never counted, and a
# pinned state is held for the whole session (exp.py --hold-state)
TRIO_STRESS = False
UPLINK_CAP_KBIT: int | None = None        # --uplink-cap-kbit (levi 02:48, diag only): trio_slots.uplink_cap_cmds
TRIO_STRESS_EPISODES: int | None = None   # --trio-stress --episodes: a held session's base episodes (decode_stress --short)


def steer_env(platform: str, troot) -> dict:
    """The steering env one trio session gets (prereg d32a48ce; levi 20:58): the table's path for a steered platform,
    LVDC_STEER_SKIPPED for one --trio-steer-platforms leaves out (the base schedule, logged), nothing without --trio-steer."""
    if not TRIO_STEER:
        return {}
    if TRIO_STEER_PLATFORMS is None or platform in TRIO_STEER_PLATFORMS:
        import steering
        return {"LVDC_STEER_TABLE": str(Path(troot) / "_summaries" / steering.TABLE_NAME)}
    return {"LVDC_STEER_SKIPPED": STEER_SKIPPED}


STEER_PLATFORMS_ALL = ("youtube", "twitch", "tiktok")


def _data_root(environ) -> Path:
    """The data root a launch with `environ` uses (LVDC_DATA_ROOT, else the repo's data dir; _RUNS_DIR at a launch)."""
    import data_root as _drs
    return Path(environ.get("LVDC_DATA_ROOT") or _drs.REPO_DATA)


def _record_root_marker(environ) -> bool:
    """The data root carries the record marker (data_root.record_root_check, as --steer-allow-table-era's refusal)."""
    import data_root as _drs
    return bool(_drs.record_root_check(_data_root(environ))["marker"])


def steer_table_refusal(troot, platforms, era: str) -> str | None:
    """levi 22:18 (B8): a steered stratum on record never starts on a table its sessions would fall back from (a silent
    base-schedule round labelled steered). The table at <trio root>/_summaries/coverage_joint.json (steer_env's path)
    is read with steering.load_table and judged per steered platform by steering.fallback_reason, the sessions' own rule,
    with no allow-era (dry only); any fallback refuses before trio_up, named. None = every steered platform steers."""
    import steering
    path = Path(troot) / "_summaries" / steering.TABLE_NAME
    table, trec = steering.load_table(path)
    bad = []
    for pf in sorted(platforms):
        why, _ = steering.fallback_reason(table, trec, pf, era, None)
        if why:
            bad.append(f"{pf}: {why}")
    if not bad:
        return None
    return (f"steered stratum on record: the steering table {path} (sha256 {str(trec.get('sha256'))[:12]}) would fall "
            f"back to the base schedule ({'; '.join(bad)}); refused before trio_up")


def steer_preflight(args, environ, steer: bool) -> str | None:
    """levi 22:18 (B8): on record (--trio-record, or a data root carrying the record marker), a steered run's table must
    steer every steered platform (steer_table_refusal); the main block and --check-args (_static_refusal) both call it."""
    if not (steer and (args.trio_record or _record_root_marker(environ))):
        return None
    import record
    import trio_slots
    plats = ({x.strip() for x in args.trio_steer_platforms.split(",") if x.strip()} if args.trio_steer_platforms
             else set(STEER_PLATFORMS_ALL))
    return steer_table_refusal(_data_root(environ) / trio_slots.TRIO_DIR, plats, record.ERA)


def steer_balancer(platforms: set[str] | None) -> None:
    """--trio-steer's balancer side (items 4 / 5; levi 20:58): TARGET_MINUTES for the steered platforms only, and TW-H in
    twitch's candidates only when twitch is steered (an explicit --coverage-allowed list stands)."""
    import steering
    coverage_report.TARGET_MINUTES = {p: {c: v / 60.0 for c, v in t.items()} for p, t in steering.TARGETS_S.items()
                                      if platforms is None or p in platforms}
    if "twitch" not in COVERAGE_ALLOWED and (platforms is None or "twitch" in platforms):
        from utils.shaping import PLATFORM_MATRICES as _PM
        COVERAGE_ALLOWED["twitch"] = sorted(_PM["twitch"])


def tiktok_room_url(room: str) -> str:
    """A pinned tiktok room (v3 W-A): its live page, opened directly (exp.py --channel-url sets the browser's first_url, so
    the room picker is skipped)."""
    return f"https://www.tiktok.com/@{room.lstrip('@')}/live"


def parse_pin(spec: str, reason: str) -> tuple[str, dict]:
    """'twitch:channel=gothamchess,category=Chess,state=10,geometry=window:1920x1080' -> (platform, pin); the key may
    also be a slot tag (t56: 'yt2:state=10'), which pins that slot alone -> (tag, pin)."""
    import re
    key, _, rest = spec.partition(":")
    m = re.fullmatch(r"(yt|tw|tt)(\d+)", key)
    platform = {"yt": "youtube", "tw": "twitch", "tt": "tiktok"}[m.group(1)] if m else key
    pin: dict = {"reason": reason}
    for kv in filter(None, rest.split(",")):
        k, _, v = kv.partition("=")
        if k not in ("category", "channel", "room", "state", "geometry") or not v:
            raise ValueError(f"--trio-pin {spec!r}: {kv!r} (keys: category, channel, room, state, geometry)")
        pin[k] = v
    if platform not in coverage_report.CLASSES or len(pin) < 2:
        raise ValueError(f"--trio-pin {spec!r}: a platform and at least one field")
    from utils.shaping import SCENARIOS
    if pin.get("state") is not None and pin["state"] not in SCENARIOS:
        raise ValueError(f"--trio-pin {spec!r}: no shaper state {pin['state']!r}")
    # v3 W-A (levi 17:18): yt channel and tt room pins too, dry only like every pin (arg_refusal: never --trio-record)
    if pin.get("category") and platform != "twitch":
        raise ValueError(f"--trio-pin {spec!r}: category pins are twitch only")
    if pin.get("channel") and platform not in ("twitch", "youtube"):
        raise ValueError(f"--trio-pin {spec!r}: channel pins are twitch / youtube (tiktok pins a room=@user)")
    if pin.get("room") and platform != "tiktok":
        raise ValueError(f"--trio-pin {spec!r}: room pins are tiktok only")
    if platform == "twitch" and pin.get("channel") and not pin.get("category"):
        raise ValueError(f"--trio-pin {spec!r}: a channel pin names its category (exp.py's --tw-channel needs --tw-category)")
    if platform == "youtube" and pin.get("channel") and yt_channels.by_handle(pin["channel"]) is None:
        raise ValueError(f"--trio-pin {spec!r}: {pin['channel']!r} is not on the yt list (yt_channels.YT_CHANNELS)")
    if pin.get("room") and not re.fullmatch(r"@?[A-Za-z0-9._]{2,64}", pin["room"]):
        raise ValueError(f"--trio-pin {spec!r}: room {pin['room']!r} is not a tiktok @user")
    return key, pin


def coupled_geometry(p: str, geom: str | None) -> str | None:
    """Q am. 15 (3): the latest coverage target's coupled geometry when it has one (yt Q_1080p -> fullscreen), else
    the rotation's draw."""
    return (LAST_COVERAGE.get(p) or {}).get("geometry_forced") or geom


def session_matrix(p: str, summary_file: str = coverage_report.SUMMARY_FILE, excluded_geometries: tuple = (),
                   data_dir: Path | None = None, select=None, reserved: dict | None = None, key: str | None = None,
                   couple_geometry: bool = True) -> str | None:
    """The session's shaper matrix (MATRIX_RULE). The coverage record goes to data/_summaries/coverage_last.json and
    LVDC_COVERAGE_JSON for exp.py's meta.json; a failure of the rule falls back to MATRIX_IDS, recorded as such.
    data_dir: the sessions the floors count (the trio: its own root, the sessions of record; levi 00265).
    couple_geometry False (the trio, restart §7): no geometry_forced (coverage_report.pick_matrix)."""
    rec = {"platform": p, "rule": "fixed", "matrix": MATRIX_IDS.get(p)}
    root = data_dir or _RUNS_DIR
    if MATRIX_RULE == "coverage" and p in coverage_report.CLASSES:
        try:
            rec = (coverage_report.choose(root, p, COVERAGE_ALLOWED.get(p), excluded_geometries, select=select,
                                          reserved=reserved, couple_geometry=couple_geometry)
                   if excluded_geometries or select or reserved or not couple_geometry
                   else coverage_report.choose(root, p, COVERAGE_ALLOWED.get(p)))
            print(f"  coverage : matrix {rec['matrix']} (target {(rec['target'] or {}).get('key')}, {rec['why']})")
        except Exception as e:
            rec["rule_error"] = repr(e)
            print(f"  coverage rule failed ({e!r}): matrix {rec['matrix']} (fixed)")
    LAST_COVERAGE[key or p] = rec                      # t56: the trio keys it by slot tag (two slots of one platform)
    path = _RUNS_DIR / summary_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=1, default=str))
    os.environ["LVDC_COVERAGE_JSON"] = str(path)
    return rec["matrix"]


FLAGS_FILE = "_summaries/flags.jsonl"


def report_flags(p: str, since: float, runs_dir: Path | None = None, only: str | None = None) -> list[dict]:
    """After a session (supervisor 2026-10-18: a collection fault is caught on the first session): every check.json of
    `p` written since `since` (accepted or _failed) with stream-regime flags gets a FLAG line and a flags.jsonl row;
    `new` lists the flags this platform had not raised before. only: that session alone (the trio, t58 N-4: a slot's
    exit no longer re-reports its siblings' sessions)."""
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
        if only is not None and c.parent.name != only:
            continue
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
          root: Path | None = None, siblings: set | frozenset = frozenset(), probe=None) -> tuple:
    """The session's channel (yt), category (twitch) and geometry draw, and the skip records they leave (run_batch and,
    per slot, run_trio). root (the trio's data root): its skip records go there, the rotation counts its sessions only
    (the sessions of record; levi 00265), and Addendum P's leave rule, single mode's list upkeep, does not run (every
    trio write stays under data/_trio). Not-live skips (channel liveness, not a count) read both roots."""
    skip = {h for h, t in not_live.items() if now - t < NOT_LIVE_RETRY_S}
    out = root or _RUNS_DIR
    import record
    select = record.is_record if root is not None else None   # the trio counts its sessions of record only (levi)
    ident = record.counted_any_era if root is not None else None   # channel identity spans every era (restart §0)
    if p == "youtube" and root is None:
        for r in yt_channels.apply_leave_rule(_RUNS_DIR, now):
            print(f"  LIST: {r['handle']} ({r['type']}, {r['role']}) leaves the yt list ({r['evidence']}; Addendum P); "
                  f"commit it in yt_channels.py")
    yt_skips = ({**yt_channels.not_live_skips(_RUNS_DIR, now), **(yt_channels.not_live_skips(root, now) if root else {})}
                if p == "youtube" else {})
    for h, why in yt_skips.items():
        print(f"  skip {h}: {why}")
    channel = None
    if p == "youtube":
        probe = probe or yt_channels.yt_live_state
        # t56 (levi): a primary pre-checked not live for >= 6 h is unavailable for the least-collected tie, recorded;
        # each pick re-probes up to REENTRY_PROBES_PER_PICK of them, and one read live re-enters at once
        unavail = yt_channels.long_not_live([_RUNS_DIR] + ([root] if root else []), now)
        for h in sorted(unavail, key=lambda x: unavail[x]["last"])[:yt_channels.REENTRY_PROBES_PER_PICK]:
            c = yt_channels.by_handle(h) or {}
            state = probe(h)
            (out / "_summaries").mkdir(parents=True, exist_ok=True)
            with open(out / yt_channels.PRECHECK_SKIPS_FILE, "a") as fh:
                fh.write(json.dumps({"ts": time.time(), "handle": h, "type": c.get("type"), "role": c.get("role"),
                                     "state": state, "reentry_probe": True}) + "\n")
            if state == "live":
                unavail.pop(h)
        # pre-session liveness probe (between sessions, outside capture; fail open; not a strike); a running sibling's
        # channel is never taken (t56, levi: siblings never play the same stream at once)
        # trio rotation hygiene (levi / supervisor): a channel with 2 trio startup_failures is set aside until a manual check
        import not_usable
        na = not_usable.set_aside(root, "youtube") if root is not None else {}
        channel, pre_skips = yt_channels.pick_live_channel(out, exclude=skip | set(yt_skips) | set(siblings) | set(unavail) | set(na),
                                                           select=ident, probe=probe)
        try:                                       # the rotation's view at the pick (no immediate repeat)
            (out / "_summaries").mkdir(parents=True, exist_ok=True)
            prec = {**yt_channels.pick_record(out, channel, ident),
                    **({"sibling_excluded": sorted(siblings)} if siblings else {}),
                    **({"unavailable_not_live_6h": unavail} if unavail else {}),
                    **({"not_usable": na} if na else {})}
            with open(out / yt_channels.PICKS_FILE, "a") as fh:
                fh.write(json.dumps(prec) + "\n")
            channel = {**channel, "pick": prec}    # levi 2026-10-20: the pick and its seeded tie in meta.json channel_pick
        except Exception as e:
            print(f"  (pick record failed: {e!r})")
        if pre_skips:
            (out / "_summaries").mkdir(parents=True, exist_ok=True)
            with open(out / yt_channels.PRECHECK_SKIPS_FILE, "a") as fh:
                for s in pre_skips:
                    fh.write(json.dumps(s) + "\n")
                    print(f"  pre-check: {s['handle']} not live now, skipped (not a strike)")
    # addendum O: the twitch category rotation (a category whose directory gave no playable stream is skipped
    # NOT_LIVE_RETRY_S, like a yt channel)
    # restart prereg §5 / §0: the category floor and the channel cap within it count identity over every counted trio
    # session, any era (ident: record.counted_any_era, above)
    if p == "twitch":
        import channel_cap
        capped = set((channel_cap.cap_state(root, "twitch", ident) or {}).get("capped") or ())
        category, esports_skip = tw_categories.next_category(out, exclude=skip, select=ident, capped=capped)
        category = {**category, "pick": dict(tw_categories.LAST_PICK)}   # meta.json channel_pick.category (trio)
    else:
        category, esports_skip = None, None
    if esports_skip:      # levi: no league channel live -> the next type; the skip goes to the daily summary
        (out / "_summaries").mkdir(parents=True, exist_ok=True)
        with open(out / "_summaries" / "tw_esports_skips.jsonl", "a") as fh:
            fh.write(json.dumps(esports_skip) + "\n")
        print(f"  esports unavailable (none of {esports_skip['checked']} live): drawing {category['name']}")
    geom = (geometry.next_geometry(out, p, exclude_modes, select) if exclude_modes else geometry.next_geometry(out, p, select=select)) \
        if rotate_geometry else None
    return channel, category, geom


def _account(p: str, rc: int, before: int, channel: dict | None, category: dict | None, now: float,
             paused: dict, fails: dict, frozen: dict, not_live: dict, runs_dir: Path | None = None,
             key: str | None = None, passed: bool | None = None, qa_cause: str | None = None) -> None:
    """A finished session's exit code into the batch state: login-wall pause, not-a-failure aborts, the not-live skip,
    the consecutive-failure pause (run_batch and, per slot, run_trio). The trio (t58 N-4) counts a slot's own streaks
    (fails / frozen keyed by `key`, the slot tag) and passes `passed`, its own session's result: a sibling's PASS on the
    platform no longer resets them; the pause stays per platform. levi 87562afd (#12): a session whose check.json carries
    qa_fail_cause = ad_dominated (`qa_cause`) leaves the failure streak as it was: it never pauses the platform, so it
    never stops the round or the window, and it is no failure of the channel (no not-live mark: it exits 0)."""
    k = key or p
    who = p if k == p else f"{p} (slot {k})"
    if rc == EXIT_LOGIN_WALL:
        paused[p] = "login wall (fix the profile template, then rerun)"
    elif rc in NOT_A_FAILURE:
        frozen[k] += 1
        print(f"  {NOT_A_FAILURE[rc]} on {who} ({frozen[k]} such aborts in a row): not counted as a failure; "
              f"next session re-picks")
        if frozen[k] >= MAX_CONSECUTIVE_FROZEN:
            paused[p] = f"{frozen[k]} consecutive player_frozen / room_ended / player_dead aborts on {who}"
    elif rc != 0 and channel:
        not_live[channel["handle"]] = now
    elif rc != 0 and category:
        not_live[category.get("channel") or category["name"]] = now
    if rc not in NOT_A_FAILURE:
        frozen[k] = 0
        ok = passed if passed is not None else (
            (accepted_sessions(p, runs_dir, trio_select()) if runs_dir else accepted_sessions(p)) > before)
        if not ok and qa_cause == "ad_dominated":
            print(f"  qa_fail_cause ad_dominated on {who}: not counted as a failure (streak {fails[k]} kept)")
        else:
            fails[k] = 0 if ok else fails[k] + 1
    if fails[k] >= MAX_CONSECUTIVE_FAIL and p not in paused:
        paused[p] = f"{fails[k]} consecutive sessions without a PASS on {who}"
    if p in paused:
        print(f"  PAUSED {p}: {paused[p]}")


def _offloads_or_exit(iface: str | None = None, ensure=None) -> dict:
    """t55: the capture interface's offloads off before the first session (each session also ensures and records
    them); refused: a loud stop, no session started."""
    import offloads
    rec = (ensure or offloads.ensure_off)(iface or offloads.IFACE_DEFAULT)
    print(f"  capture offloads on {rec['iface']}: {'off' if rec['ok'] else 'NOT OFF'} {json.dumps(rec['after'])}")
    if not rec["ok"]:
        print(f"\n  t55: the capture offloads could not be switched off ({json.dumps(rec['calls'])}): not starting.")
        sys.exit(EXIT_OFFLOADS)
    return rec


def run_batch(targets: dict[str, int], auto: bool, yt_matrix: str, rotate_geometry: bool = True) -> None:
    """Collect until every platform has its target of PASS sessions. Next platform = next_platform(): the one furthest
    behind its target (accepted / target; BATCH_ORDER breaks ties), at most MAX_CONSECUTIVE_SAME_PLATFORM in a row, so
    platforms advance together over the week. Nothing here
    changes what a session collects; it only sequences exp.py runs and recovers from failures."""
    _offloads_or_exit()
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
        fi = (LAST_COVERAGE.get(p) or {}).get("initial_state_forced") if MATRIX_RULE == "coverage" else None
        if fi:                                                # Q am. 15 (5): the class target starts in its band
            print(f"  initial state {fi['state']} (coupled to the coverage target {fi['class']}: band {fi['band']})")
        try:
            rc = _run_collection(p, initial_state=fi["state"] if fi else most_needed_state(p, _RUNS_DIR, yt_matrix=mid, matrix_id=mid),
                                 initial_state_source="coverage" if fi else "balance",
                                 episodes=session_episodes(EPISODES_V2.get(p, 5), mid), auto=auto, yt_matrix=yt_matrix,
                                 channel=channel, geom=geom, tw_category=category, matrix=mid)
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
        _account(p, rc, before, channel, category, now, paused, fails, frozen, not_live,
                 qa_cause=_qa_fail_cause(_RUNS_DIR, p, since=t_session))
        _summary()
    print("\nBatch finished." + (f" Paused: {paused}" if paused else ""))
    for p in targets:
        print(f"  {p}: {accepted_sessions(p)}/{targets[p]} accepted")
    _summary()


# ---------------------------------------------------------------------------
# t53 --concurrent: the trio (design LVQoE concurrent_trio_design_peter_2026-10-18.md, A1 rolling slots)
# ---------------------------------------------------------------------------

TRIO_STAGGER_S = 30.0     # at least this long between two slot starts (Chrome launches and profile clones not at once)
# restart prereg §10 (levi 00138): the record's window is RECORD_WINDOW_MIN minutes (--trio-record refuses any other);
# a slot's next session fits when now + its drawn length + WINDOW_ALLOWANCE_S (startup + teardown) <= the deadline
RECORD_WINDOW_MIN = 120.0
WINDOW_ALLOWANCE_S = 180.0


def window_fits(now: float, minutes: float, deadline: float, allowance_s: float | None = None) -> bool:
    """Restart §10: a session of `minutes` started at `now` fits when now + its length + the allowance <= the deadline
    (a session ending exactly at the deadline fits)."""
    return now + minutes * 60 + (WINDOW_ALLOWANCE_S if allowance_s is None else allowance_s) <= deadline
SESSION_RANGE: tuple | None = None     # --session-range (dry runs only, never the record): the length law's bounds
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


def _reserved(tag: str, T, running: dict) -> dict:
    """t56: the classes the running sibling slots of tag's platform already target (one each). Restart §7: the target
    no longer depends on any geometry (the fullscreen token's Q_1080p reservation is gone with the coupling)."""
    import collections
    p, out = T.SLOTS[tag].platform, collections.Counter()
    for t2, r in running.items():
        if t2 != tag and T.SLOTS[t2].platform == p and r.get("target"):
            out[r["target"]] += 1
    return dict(out)


def pin_geometry(pin: dict, geom: str | None, running: dict, tag: str) -> tuple[str | None, dict]:
    """A pin's geometry, through the one-fullscreen token (t58 N-6, brad's audit: the pin was applied after the token
    check): a pinned fullscreen geometry while a sibling holds fullscreen is deferred, the drawn geometry kept and the
    deferral recorded in the pin (geometry_deferred)."""
    pg = pin.get("geometry")
    if pg and pg.startswith("fullscreen") and any(str(r.get("geom") or "").startswith("fullscreen")
                                                  for t2, r in running.items() if t2 != tag):
        print(f"  PINNED {tag}: geometry {pg} deferred (a sibling holds the fullscreen token); {geom} instead")
        return geom, {**pin, "geometry_deferred": pg}
    return pg or geom, pin


def _trio_refill(tag: str, T, targets: dict, started: dict, running: dict, paused: dict, not_live: dict,
                 rotate_geometry: bool, yt_matrix: str, capacity_kbit: float | None, run_id: str, summaries: Path,
                 spawn, floor: dict | None = None, stop_signal: list | None = None, window: dict | None = None,
                 idle: set | None = None) -> bool:
    """Start the next session in slot `tag` (run_trio); False when its preflight failed (the trio drains). In a window
    (restart §10) the slot draws its next session's length first and, when it does not fit before the window's
    deadline, idles to the window's end (added to `idle`, a window_idle event): no truncation, no re-draw."""
    s, troot = T.SLOTS[tag], _RUNS_DIR / T.TRIO_DIR
    p = s.platform
    # restart prereg §4: the session's length is drawn first (U(10, 40) min, seeded by the run, the slot and its session
    # index); a held state (decode_stress) runs base episodes (exp.py: hold is base)
    held = TRIO_STRESS and (TRIO_PINS.get(tag) or TRIO_PINS.get(p) or {}).get("state")
    slen = None if held else draw_session_minutes(f"{run_id}:{s.tag}:{started[tag] + 1}")
    if slen and SESSION_RANGE:          # a dry run's law (--session-range, never the record): the same draw rescaled
        lo, hi = SESSION_RANGE
        slen = {**slen, "session_range": [lo, hi],
                "minutes": round(lo + (hi - lo) * (slen["minutes"] - 10.0) / 30.0, 4)}
    if slen and REPLAY:                  # v3 W-A: the replayed schedule fixes the length (exp.py ends with it)
        import utils.netStat as _ns
        slen = {**slen, "minutes": round(len(_ns.REPLAY_STATES) * (REPLAY.get("step_s") or _ns.REPLAY_STEP_S) / 60.0, 4),
                "replay": True}
    if window and slen and not window_fits(time.time(), slen["minutes"], window["deadline"]):
        T.slots_log_append(troot / T.SLOTS_LOG, {"event": "window_idle", "slot": tag, "run": run_id, "t": time.time(),
                                                 "reason": "does not fit", "minutes": slen["minutes"],
                                                 "allowance_s": WINDOW_ALLOWANCE_S, "deadline": window["deadline"]})
        print(f"  TRIO {tag}: the next session's {slen['minutes']:.1f} min does not fit the window: idle to its end")
        if idle is not None:
            idle.add(tag)
        return True
    gb = free_gb(_RUNS_DIR)
    if gb < MIN_FREE_GB:
        print(f"\n  Data volume {gb:.0f} GB free < {MIN_FREE_GB} GB: draining the trio.")
        raise _TrioStop(EXIT_LOW_DISK)
    floor = floor or T.staging_floor_gb(_RUNS_DIR, MIN_FREE_GB)
    # t58: the trio stages on the data volume, so its raised floor is the volume's; the rootfs keeps single mode's
    pf = {**preflight.run(_RUNS_DIR, max(MIN_FREE_GB, floor["floor_gb"])), "trio_staging_floor": floor}
    pf_path = summaries / f"preflight_last_{s.tag}.json"
    pf_path.write_text(json.dumps(pf, indent=1))
    if not pf["ok"]:
        print(f"\n  PREFLIGHT FAILED for the {p} slot ({', '.join(pf['problems'])}): draining the trio. "
              f"{json.dumps({k: pf['checks'][k] for k in pf['problems']})}")
        return False
    before, now = accepted_sessions(p, troot, trio_select()), time.time()
    # the pick and the coverage rule count the trio's own sessions (data/_trio: the sessions of record, levi 00265); without
    # --trio-yt-fullscreen no fullscreen geometry is drawn and a class needing one is unreachable (supervisor 2026-10-19)
    sib = {(r.get("channel") or {}).get("handle") for t2, r in running.items() if t2 != tag and r.get("channel")} - {None}
    channel, category, _ = _pick(p, not_live, now, False, T.EXCLUDED_GEOMETRIES, troot, siblings=sib)
    import record
    reserved = _reserved(tag, T, running)
    yt_fs = p == "youtube" and TRIO_YT_FULLSCREEN
    mid = session_matrix(p, f"{T.TRIO_DIR}/_summaries/coverage_last_{s.tag}.json",
                         () if yt_fs else T.EXCLUDED_GEOMETRIES, data_dir=troot, select=record.is_record,
                         reserved=reserved or None, key=tag, couple_geometry=False)
    # restart §7: the geometry drawn per session, seeded, independent of the target (geometry.draw_geometry); a
    # fullscreen draw while a sibling holds the token (design v4: one at a time) is redrawn among the other entries
    seed = f"{SESSION_DRAW_SEED}:{run_id}:{s.tag}:{started[tag] + 1}"
    fs_held = any(str(r.get("geom") or "").startswith("fullscreen") for t2, r in running.items() if t2 != tag)
    gdraw = geometry.draw_geometry(p, seed, () if yt_fs else T.EXCLUDED_GEOMETRIES, fs_held) if rotate_geometry else None
    geom = (gdraw or {}).get("geometry")
    if (gdraw or {}).get("token_deferred"):
        print(f"  geometry {geom} (drew {gdraw['token_deferred']}; a sibling holds the fullscreen token)")
    # restart §8: twitch's low-latency setting, drawn per session (tw_latency.py), applied before playback by exp.py
    ll = tw_latency.draw(seed) if p == "twitch" else None
    # Q am. 15 (5), as single mode (run_batch): a tier-class target starts in its band (the merge had kept the balance rule)
    fi = (LAST_COVERAGE.get(tag) or {}).get("initial_state_forced") if MATRIX_RULE == "coverage" else None
    # t53 2026-10-19 (levi; supervisor): the balance start state counts the trio's sessions of record too (00265), as the
    # pick and the coverage rule do; at c1f9181 it read the single root (disclosed in the FROZEN log)
    init = fi["state"] if fi else most_needed_state(p, troot, yt_matrix=mid, matrix_id=mid, select=record.is_record)
    init_source = "coverage" if fi else "balance"
    tt_room = None
    pin = TRIO_PINS.get(tag) or TRIO_PINS.get(p)        # a tag's own pin, else its platform's
    if pin:                             # a recorded round pin (levi 00261), never silent
        if pin.get("category"):
            category = {"name": pin["category"], "type": "pinned", "channel": pin.get("channel")}
        if p == "youtube" and pin.get("channel"):        # v3 W-A: the list entry itself (handle + type), not a draw
            channel = yt_channels.by_handle(pin["channel"])
        if p == "tiktok" and pin.get("room"):
            tt_room = tiktok_room_url(pin["room"])
        if pin.get("state"):
            init, init_source = pin["state"], "pin"
        geom, pin = pin_geometry(pin, geom, running, tag)
        print(f"  PINNED {tag}: {json.dumps(pin)}")
    if gdraw is not None and geom != gdraw["geometry"]:
        gdraw = {**gdraw, "pinned": geom}                 # a recorded round pin overrides the draw (pick_pinned too)
    hold = TRIO_STRESS and (pin or {}).get("state")      # decode_stress: the pinned state held for the whole session
    # a schedule's episodes scale with the drawn length (Q am. 17); @fast's episode length is drawn per session
    # (restart §2b, the same key); a held state runs base episodes
    fe = draw_fast_episode_s(slen["key"]) if slen and (mid or "").partition("@")[2] == "fast" else None
    if fe:
        slen = {**slen, "fast_episode": fe}
    eps = EPISODES_V2.get(p, 5) if hold else session_episodes(slen["minutes"], mid, fe["episode_s"] if fe else None)
    if slen:                                              # §4's covariate: the schedule's own time (levi 00138)
        slen = {**slen, "episodes": eps, "scheduled_s": round(
            eps * schedule_of(mid, base_episodes=slen["minutes"], episode_s=fe["episode_s"] if fe else None)["episode_s"], 1)}
    if hold and TRIO_STRESS_EPISODES:                    # decode_stress --short / --minutes (never a counted run)
        eps = TRIO_STRESS_EPISODES
    cmd = _collection_cmd(p, initial_state=init, initial_state_source=init_source, episodes=eps, auto=True, yt_matrix=yt_matrix,
                          channel=channel, geom=geom, tw_category=category, matrix=mid, tt_room=tt_room)
    cmd += ["--slot", s.tag] + (["--link-capacity-kbit", str(capacity_kbit)] if capacity_kbit else [])
    if slen:
        cmd += ["--session-minutes", str(slen["minutes"])]
    if fe:
        cmd += ["--fast-episode-s", str(fe["episode_s"])]
    if ll is not None:
        cmd += ["--tw-low-latency", ll["drawn"]]
    if hold:
        cmd += ["--hold-state", pin["state"]]
    if REPLAY and not hold:                               # v3 W-A: every session replays the same schedule
        cmd += ["--replay-schedule", REPLAY["seed"]] + (["--replay-step-s", str(REPLAY["step_s"])] if REPLAY.get("step_s") else [])
    lever_args = lever_cmd(p, hold)
    if lever_args is None:
        raise RuntimeError(f"lever window but no lever params for {p}; refused")   # load_params guarantees all three
    cmd += lever_args
    # shots: CDP in the "cdp" layout (three windows on one screen), X11 crops in the "fb" layout (design §4), each
    # window's own XComposite pixmap in the "composite" layout (pilot-1, probe v3)
    env = {**os.environ, "LVDC_TRIO_RUN": run_id, "LVDC_PREFLIGHT_JSON": str(pf_path),
           "LVDC_TRIO_SLOTS": ",".join(f"{k}={v}" for k, v in T.composition_of(T.SLOTS).items()),
           "LVQOE_SHOT_SOURCE": TRIO_SHOT_SOURCE.get(os.environ.get("LVDC_TRIO_LAYOUT") or "cdp", "cdp")}
    env.pop("LVDC_PICK_PINNED", None)
    env.pop("LVDC_CHANNEL_PICK", None)
    env.pop("LVDC_SESSION_LENGTH", None)
    if slen:
        env["LVDC_SESSION_LENGTH"] = json.dumps(slen)
    for k, v in (("LVDC_GEOMETRY_DRAW", gdraw), ("LVDC_TW_LOW_LATENCY", ll)):   # restart §7 / §8: meta.json records
        env.pop(k, None)
        if v is not None:
            env[k] = json.dumps(v)
    if (channel or {}).get("pick"):
        env["LVDC_CHANNEL_PICK"] = json.dumps(channel["pick"], default=str)
    elif (category or {}).get("pick"):            # restart prereg §5: the category draw, its floor and seeded tie
        env["LVDC_CHANNEL_PICK"] = json.dumps({"category": category["pick"]}, default=str)
    env["LVDC_TRIO_RECORD"] = "1" if TRIO_RECORD else "0"
    env.pop("LVDC_STEER_TABLE", None)
    env.pop("LVDC_STEER_SKIPPED", None)
    env.update(steer_env(p, troot))
    env.pop("LVDC_TRIO_YT_FULLSCREEN", None)
    if yt_fs:
        env["LVDC_TRIO_YT_FULLSCREEN"] = "1"
    if pin:
        env["LVDC_PICK_PINNED"] = json.dumps({**pin, "initial_state": init, "geometry_used": geom,
                                              "category_used": (category or {}).get("name")})
    if stop_signal:                     # a stop arrived during the pick: no new session
        print(f"  TRIO {tag}: stop requested during the pick, no session started")
        return True
    started[tag] += 1
    log = troot / TRIO_LOGS / run_id / f"{s.tag}_{started[tag]}.log"
    print(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M}] TRIO {p} slot ({s.tag}) session {started[tag]}: "
          f"{before}/{targets[p]} accepted"
          + (f"; channel {channel['handle']}" if channel else "")
          + (f"; category {category['name']}" if category else "")
          + (f"; geometry {geom}" if geom else "") + (f"; {slen['minutes']:.1f} min" if slen else "")
          + f"\n  Running: {' '.join(cmd)}\n  log: {log}")
    running[tag] = {"proc": spawn(cmd, env, log), "before": before, "channel": channel, "category": category,
                    "now": now, "t": time.time(), "log": str(log), "geom": geom,
                    "target": ((LAST_COVERAGE.get(tag) or {}).get("target") or {}).get("key")}
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
             spawn=_spawn, apply=None, make=_make, sleep=time.sleep, xrandr=None, offloads=None,
             slots: dict | None = None, n_draw: dict | None = None, window_min: float | None = None) -> int:
    """t56: the slots of a composition (`slots` {platform: count}, default the trio's one each; trio_slots.build_slots),
    keyed by tag; each slot refilled when its session ends (A1); targets and accounting per platform, until every platform has its
    target of PASS trio sessions (counted in the trio's own data root, data/_trio/<platform>: single mode never sees
    them), or `max_per_slot` sessions started per slot (the pilot: 1). Without --trio-yt-fullscreen no fullscreen
    geometry is drawn, and a coverage class that needs one is unreachable (recorded). Per slot as run_batch
    per session: the pick (_pick), the per-session preflight (rootfs floor raised by A2), the coverage matrix (restart
    §7: the geometry drawn per session, never coupled to the target; §8: twitch's low-latency draw), the accounting
    (_account), the flag report; each slot's coverage / preflight record is its own
    file. Started in a pause (no session running): single mode's preclean once, then the shared tree, the unmarked
    remainder's capture, then the slots; no orchestrator-wide preclean while a slot runs (a slot cleans only its own
    pieces, exp.py slot_up). Drain (TRIO_DRAIN_FILE, or SIGINT / SIGTERM, which are also passed to every running
    session) stops refilling; once every slot has ended the default capture is merged and the shared tree removed.
    layout: "cdp" (pilot-0: windows at the origin, CDP shots) or "fb" (design §4: the X framebuffer enlarged to three
    monitor widths for the run, restored at its end; slot k's window at k x the width; X11 shots) or "composite"
    (pilot-1: windows at the origin as cdp, each shot a crop of the window's own XComposite pixmap).
    t55: the capture offloads off on iface before anything starts (offloads: the ensure function, tests).
    n_draw: the round's N draw (n_draw.draw; restart prereg §1), whose composition `slots` is: recorded in trio_up and,
    through LVDC_N_DRAW, in every session's meta.json concurrency.n_draw.
    window_min (restart §10): the run is a window of this many minutes from its trio_up; each slot refills while its
    next drawn length fits before the deadline and idles to the end otherwise (_trio_refill); recorded in trio_up
    window and meta.json concurrency.window (LVDC_TRIO_WINDOW).
    Returns the exit code: 0, or EXIT_STOPPED / EXIT_LOW_DISK / EXIT_PREFLIGHT / EXIT_OFFLOADS when that ended it."""
    import signal
    import trio_slots as T
    if slots:                                             # the run's composition, for every slot's exp.py too
        T.SLOTS.clear()
        T.SLOTS.update(T.build_slots(slots))
    os.environ["LVDC_TRIO_SLOTS"] = ",".join(f"{k}={v}" for k, v in T.composition_of(T.SLOTS).items())
    os.environ.pop("LVDC_N_DRAW", None)
    if n_draw:
        os.environ["LVDC_N_DRAW"] = json.dumps(n_draw)
    os.environ.pop("LVDC_TRIO_WINDOW", None)
    window, idle, busy_s = None, set(), {}
    apply = apply or T.apply
    try:
        _offloads_or_exit(iface, offloads)
    except SystemExit:
        return EXIT_OFFLOADS
    os.environ["LVDC_CAPTURE_IFACE"] = iface              # each slot's exp.py ensures and records them on the same one
    global TRIO_RUN_ID
    run_id = TRIO_RUN_ID = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if not _RUNS_DIR.is_dir():           # never create data/ on the root filesystem (a missing data link)
        print(f"\n  TRIO: the data dir {_RUNS_DIR} does not exist: not starting the trio.")
        return EXIT_PREFLIGHT
    troot = _RUNS_DIR / T.TRIO_DIR                          # trio sessions' own data root (never pooled silently)
    summaries = troot / "_summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    slots_log, drain_path = troot / T.SLOTS_LOG, troot / TRIO_DRAIN_FILE
    paused: dict[str, str] = {}
    fails = {tag: 0 for tag in T.SLOTS}                  # t58 N-4: a slot's own streaks (the pause stays per platform)
    frozen = {tag: 0 for tag in T.SLOTS}
    not_live: dict[str, float] = {}
    started = {tag: 0 for tag in T.SLOTS}
    running: dict[str, dict] = {}                         # by slot tag
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
    platforms = {s.platform for s in T.SLOTS.values()}
    pre = T.trio_preflight([p for p in targets if p in platforms], repo, tpl, _RUNS_DIR)
    (summaries / "trio_preflight_last.json").write_text(json.dumps(pre, indent=1))
    if not pre["ok"]:                    # nothing touched: no tree, no capture, no session
        print(f"\n  TRIO PREFLIGHT FAILED ({', '.join(pre['problems'])}): not starting the trio.")
        return EXIT_PREFLIGHT
    floor = T.staging_floor_gb(_RUNS_DIR, MIN_FREE_GB)   # once per run (it stats every pcap); the data volume's
    old = {s: signal.signal(s, _on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    preclean()
    for s in T.SLOTS.values():
        apply(T.slot_down_cmds(s, iface, T.nft_handle_of(s)), check=False)
    apply(T.shared_down_cmds(iface), check=False)
    xrandr = xrandr or _xrandr
    screen = fb = dstage = ddir = ct_before = None
    try:
        apply(T.shared_up_cmds(iface))
        uplink_cap = None
        if UPLINK_CAP_KBIT:                          # levi 02:48: the uplink-bound positive control (stress runs only)
            uplink_cap = {"kbit": UPLINK_CAP_KBIT, "cmds": T.uplink_cap_cmds(iface, UPLINK_CAP_KBIT)}
            apply(uplink_cap["cmds"])
        # P3 (levi 00120): the default class's capture IFBs with their segmentation / coalescing offloads off, as every
        # slot's (exp.py); refused: no session starts (the tree comes down in the finally)
        import offloads as _off
        ensure_ifb = offloads if offloads is not None else (lambda d: _off.ensure_off(d, features=_off.IFB_OFF))
        ifb_off = {d: ensure_ifb(d) for d in ("lvi_def", "lvo_def")}
        if not all(r.get("ok") for r in ifb_off.values()):
            print(f"  TRIO: the default capture IFBs' offloads are not off ({json.dumps(ifb_off)}): no session starts")
            end_rc = EXIT_OFFLOADS
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
        if window_min:                                    # restart §10: the window runs from here
            t_up = time.time()
            window = {"min": window_min, "t0": t_up, "deadline": t_up + window_min * 60}
            os.environ["LVDC_TRIO_WINDOW"] = json.dumps(window)
        T.slots_log_append(slots_log, {"event": "trio_up", "run": run_id, "t": time.time(), "iface": iface,
                                       "capacity_kbit": capacity_kbit, "targets": targets, "max_per_slot": max_per_slot,
                                       "slots": {k: s.record() for k, s in T.SLOTS.items()},
                                       "composition": T.composition_of(T.SLOTS),
                                       **({"n_draw": n_draw} if n_draw else {}),
                                       **({"stratum": json.loads(os.environ["LVDC_WINDOW_STRATUM"])}
                                          if os.environ.get("LVDC_WINDOW_STRATUM") else {}),
                                       **({"window": window} if window else {}),
                                       "conntrack_timeouts_before": ct_before, "conntrack_timeouts": T.conntrack_timeouts(),
                                       "conntrack_set": ct_set[0], "resolver_cache_flushed": flush[0],
                                       "layout": layout, "screen": screen, "framebuffer": fb, "ifb_offloads": ifb_off,
                                       "uplink_cap": uplink_cap})
        print(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M}] TRIO {run_id} up: {targets}, max per slot {max_per_slot}, "
              f"layout {layout}")
        while True:
            for tag, r in list(running.items()):
                rc = r["proc"].poll()
                if rc is None:
                    continue
                del running[tag]
                busy_s[tag] = busy_s.get(tag, 0.0) + time.time() - r["t"]
                p = T.SLOTS[tag].platform
                T.slots_log_append(slots_log, {"event": "exit", "slot": tag, "rc": rc, "t": time.time()})
                print(f"[{dt.datetime.now():%H:%M}] TRIO {tag}: exp.py rc {rc} (log {r['log']})")
                name = _slot_session(slots_log, tag, run_id)
                try:
                    report_flags(p, r["t"], troot, only=name)
                except Exception as e:
                    print(f"  (flag report failed: {e!r})")
                if rc == EXIT_STOPPED:
                    print(f"  {p}: session stopped (SIGINT / SIGTERM): draining the trio")
                    end_rc = end_rc or EXIT_STOPPED
                else:
                    _account(p, rc, r["before"], r["channel"], r["category"], r["now"], paused, fails, frozen, not_live,
                             troot, key=tag, passed=_session_passed(troot, p, name, trio_select()),
                             qa_cause=_qa_fail_cause(troot, p, name))
            if window:      # §10 (levi 00138): a paused slot idles to the window's end, its reason recorded; a second
                for tag, s in T.SLOTS.items():                # one is a stop-and-read
                    if s.platform in paused and tag not in idle and tag not in running:
                        idle.add(tag)
                        T.slots_log_append(slots_log, {"event": "window_idle", "slot": tag, "run": run_id,
                                                       "t": time.time(), "reason": f"paused: {paused[s.platform]}"})
                n_paused = sum(1 for s in T.SLOTS.values() if s.platform in paused)
                if n_paused > 1 and not end_rc:
                    print(f"  TRIO window: {n_paused} slots paused ({paused}): stop and read")
                    T.slots_log_append(slots_log, {"event": "window_stop", "run": run_id, "t": time.time(),
                                                   "reason": f"{n_paused} slots paused", "paused": dict(paused)})
                    end_rc = EXIT_WINDOW_READ
            draining = bool(stop_signal) or end_rc != 0 or drain_path.exists()
            # a window runs to time: targets and --max-per-slot never stop its slots (levi 00138)
            done = {p: (not window) and accepted_sessions(p, troot, trio_select()) >= targets[p] for p in targets}
            todo = [tag for p in BATCH_ORDER for tag, s in T.SLOTS.items() if s.platform == p and p in targets
                    and p not in paused and (window or max_per_slot is None or started[tag] < max_per_slot)
                    and not done[p] and tag not in idle]
            if not running and (draining or not todo):
                break
            for tag in ([] if draining else todo):
                if tag in running or time.time() - last_start < stagger_s:
                    continue
                try:
                    if not _trio_refill(tag, T, targets, started, running, paused, not_live, rotate_geometry, yt_matrix,
                                        capacity_kbit, run_id, summaries, spawn, floor, stop_signal, window, idle):
                        end_rc = end_rc or EXIT_PREFLIGHT
                        break
                except _TrioStop as e:
                    end_rc = end_rc or e.rc
                    break
                except Exception as e:      # never a crash under running siblings: drain instead
                    print(f"  TRIO {tag} slot could not start a session ({e!r}): draining the trio")
                    end_rc = end_rc or 1
                    break
                if tag not in idle:                   # a window-idle slot started nothing
                    last_start = time.time()
            sleep(poll_s)
    finally:
        for tag, r in running.items():      # only after an unexpected error: every session finalises before the teardown
            print(f"  TRIO: waiting for the {tag} session to finalise before the teardown")
            try:
                r["proc"].send_signal(signal.SIGTERM)
                r["proc"].wait(timeout=900)
            except Exception as e:
                print(f"  ({tag}: {e!r})")
        if ddir is not None:
            make(["stop-capture"] + T.default_capture_vars(dstage))
            make(["merge-capture", f"SESSION_DIR={ddir}"] + T.default_capture_vars(dstage))
        down = apply(T.shared_down_cmds(iface), check=False)
        ct_restored = None
        if ct_before and ct_before.get("nf_conntrack_udp_timeout_stream"):
            ct_restored = apply([f"sysctl -w net.netfilter.nf_conntrack_udp_timeout_stream="
                                 f"{ct_before['nf_conntrack_udp_timeout_stream']}"], check=False)[0]
        restored = xrandr(f"{screen[0]}x{screen[1]}") if screen else None
        t_down = time.time()
        wrec = {}
        if window:          # §10 bookkeeping (levi 00138): the actual span, and each slot's busy and idle seconds
            span = t_down - window["t0"]
            wrec = {"window": {**window, "t_end": t_down, "span_s": round(span, 1),
                               "busy_s": {k: round(busy_s.get(k, 0.0), 1) for k in T.SLOTS},
                               "idle_s": {k: round(max(0.0, span - busy_s.get(k, 0.0)), 1) for k in T.SLOTS}}}
        T.slots_log_append(slots_log, {"event": "trio_down", "run": run_id, "t": t_down, "end_rc": end_rc,
                                       "signal": stop_signal[:1], "teardown_failed": [x["cmd"] for x in down if x["rc"]],
                                       "framebuffer_restored": restored, "conntrack_restored": ct_restored, **wrec})
        _consume_drain("done")
        for s, h in old.items():
            signal.signal(s, h)
    end_rc = end_rc or (EXIT_STOPPED if stop_signal else 0)
    print(f"\nTRIO {run_id} finished (rc {end_rc})." + (f" Paused: {paused}" if paused else ""))
    for p in targets:
        print(f"  {p}: {accepted_sessions(p, troot, trio_select())}/{targets[p]} trio sessions accepted, "
              f"{sum(n for tag, n in started.items() if T.SLOTS[tag].platform == p)} started")
    return end_rc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument("--tw-matrix", choices=["TW-M", "TW-L", "TW-H", "current"], default="TW-M",
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
    parser.add_argument("--slots", default=None, metavar="youtube=2,twitch=2,tiktok=2",
                        help="--concurrent (t56): the composition, one slot per count (tags yt1, yt2, tw1 ...; at most "
                             "15); default the trio's, one each. Fixed for the run and recorded in every session.")
    parser.add_argument("--expect-stratum", choices=("steered", "unsteered", "lever"), default=None,
                        help="collector v3 B5 (levi 18:02 / 18:57): the window's stratum as rounds_ref_n computed it "
                             "(stratum_draw.py: permuted blocks of 4 over k = the earlier windows of the era and record flag "
                             "that started a session); orchestrate recomputes it and refuses a mismatch before trio_up. "
                             "Required with --trio-record.")
    parser.add_argument("--stratum-force", choices=("steered", "unsteered", "lever"), default=None,
                        help="a dry / gate run's stratum, stated (never with --trio-record); recorded like a drawn one.")
    parser.add_argument("--steer-allow-table-era", default=None, metavar="ERA",
                        help="levi 19:35, DRY ONLY: steering may read a table of this era (W-B's S round on the t58 "
                             "table copy, to exercise the mechanism); refused with --trio-record and on a record root; "
                             "recorded in meta.steering {table_era, allowed_by_flag}.")
    parser.add_argument("--replay-schedule", default=None,
                        help="v3 W-A (levi 17:1x): every session replays one fixed schedule seeded by this seed (exp.py "
                             "--replay-schedule: states 10->1->10); dry only (never with --trio-record), steering off.")
    parser.add_argument("--replay-step-s", type=float, default=None,
                        help="v3 W-A: the replayed schedule's step (exp.py default 79 s, a 25 min session).")
    parser.add_argument("--lever-params", default=None,
                        help="collector v3 B4 x B5: the lever window's values per platform, JSON {youtube|twitch|tiktok: "
                             "{lambda_per_s, d_lo_s, d_hi_s}} (the frozen line's; lever_params.load_params). Needed when the "
                             "window's stratum is lever (drawn or forced); only with --expect-stratum / --stratum-force.")
    parser.add_argument("--lever-params-sha256", default=None,
                        help="levi 16:30 / 16:32: the sha256 of the --lever-params file as named in the dated prereg line "
                             "(rounds_ref_n's pinned constant); required with --lever-params, refused on a mismatch.")
    parser.add_argument("--n-draw", action="store_true",
                        help="--concurrent (restart prereg §1): draw this round's N from {3, 4, 5} (weights 0.2 / 0.3 / "
                             "0.5, seeded by the era, the record flag and the round's index in the slots log; n_draw.py) "
                             "and run its fixed composition; recorded in trio_up and meta.json concurrency.n_draw. Not "
                             "with --slots. A counted run (--trio-record) needs it.")
    parser.add_argument("--n-support", default=None, metavar="3,4,5",
                        help="--n-draw: the support (an N that failed its scaling gate is left out; the weights "
                             "renormalise). Default 3,4,5.")
    parser.add_argument("--n-exclude", default=None, metavar="4:tiktok,5:youtube",
                        help="--n-draw: compositions that failed their decode stress, out of their rotation (N = 4's "
                             "doubled / N = 5's single platform); stated in every draw. Default none.")
    parser.add_argument("--n-change-sign", default=None, metavar="'levi 00140'",
                        help="--n-draw, a counted run: the signed reference for narrowing the era's support / adding an "
                             "exclusion (levi 00138: they are otherwise frozen at the era's last record draw's); recorded.")
    parser.add_argument("--window-min", type=float, default=None, metavar="120",
                        help="--concurrent (restart prereg §10): the run is a window of this many minutes (the round "
                             "for every per-round gate; N drawn and held for it with --n-draw); each slot refills while "
                             "its next session's drawn length fits before the window's end and idles to the end "
                             "otherwise (no truncation, no re-draw). Default off: refill until targets / --max-per-slot.")
    parser.add_argument("--session-range", default=None, metavar="4,8",
                        help="--concurrent, an uncounted run only (e.g. the refill dry run): each session's length is "
                             "the restart §4 draw rescaled from U(10, 40) to U(LO, HI) minutes, recorded "
                             "(session_length.session_range). Refused with --trio-record.")
    parser.add_argument("--trio-pin", action="append", default=[], metavar="PLATFORM|TAG:k=v,...",
                        help="--concurrent: a recorded pin of a slot's pick (levi 00261; default off), e.g. "
                             "'twitch:channel=gothamchess,state=10,geometry=window:1920x1080' (keys: category, channel "
                             "[twitch], state, geometry); needs --trio-pin-reason; each session records meta pick_pinned. "
                             "It holds for every refill of that platform in the run (every session under a slot cap > 1).")
    parser.add_argument("--trio-pin-reason", default=None, help="--trio-pin: the reason recorded with each pinned pick.")
    parser.add_argument("--trio-record", action="store_true",
                        help="--concurrent: a counted run: its sessions are of record (meta concurrency.record, era "
                             "record.ERA); steering counts only such sessions. Pilots and re-runs: leave it off.")
    parser.add_argument("--uplink-cap-kbit", type=int, default=None,
                        help="--trio-stress only (levi 02:48): police all non-management downloads on the interface at "
                             "this rate before the slots, a slower house link (trio_slots.uplink_cap_cmds); in trio_up.")
    parser.add_argument("--trio-stress", action="store_true",
                        help="--concurrent, t56 decode_stress: sessions under data/_trio_stress (never counted; not with "
                             "--trio-record); a --trio-pin state is held for the whole session (exp.py --hold-state).")
    parser.add_argument("--trio-steer-platforms", default=None,
                        help="with --trio-steer (levi 20:58): comma list of the platforms steered (youtube, twitch, "
                             "tiktok); the rest run the base schedule, logged. Default: every platform")
    parser.add_argument("--trio-steer", action="store_true",
                        help="--concurrent (prereg d32a48ce; default off): each session's shaper dwell weights drawn at "
                             "session start from <trio root>/_summaries/coverage_joint.json (the last post's deficit)")
    parser.add_argument("--trio-yt-fullscreen", action="store_true",
                        help="--concurrent (t53 (a); default off): the yt slot goes fullscreen when its coverage target "
                             "couples it (Q_1080p), with a focus keeper and on-top-gated root grabs (browser.py).")
    parser.add_argument("--trio-layout", choices=sorted(TRIO_SHOT_SOURCE), default="cdp",
                        help="--concurrent: cdp = windows at the origin, CDP shots (pilot-0); fb = the X framebuffer "
                             "enlarged to three monitor widths, one window per width, X11 shots (design §4; probe v1-v3: "
                             "GNOME Shell keeps windows on the monitor); composite = windows at the origin, each shot a "
                             "crop of the window's own XComposite pixmap (pilot-1, probe v3 8d0270c).")
    parser.add_argument("--max-per-slot", type=int, default=None,
                        help="--concurrent: stop refilling a slot after this many sessions (the pilot: 1).")
    parser.add_argument("--iface", default=os.environ.get("LVDC_IFACE", "wlp0s20f3"),
                        help="--concurrent: the collection interface (the Makefile's IFACE).")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print balance report without collecting.",
    )
    return parser


def lever_cmd(platform: str, hold) -> list[str] | None:
    """collector v3 B4 x B5: the session's --lever argument in a lever window ([] outside one, or for a held decode_stress
    session, which never levers); None when the window is a lever window without values for this platform."""
    ws = json.loads(os.environ["LVDC_WINDOW_STRATUM"]) if os.environ.get("LVDC_WINDOW_STRATUM") else None
    if not ws or ws.get("stratum") != "lever" or hold:
        return []
    params = (ws.get("lever_params") or {}).get(platform)
    return ["--lever", json.dumps(params, sort_keys=True)] if params else None


def stratum_apply(sd: dict | None, expect: str | None, force: str | None, trio_steer: bool,
                  lever_params: tuple[dict, str] | None = None) -> tuple[dict | None, bool, str | None]:
    """collector v3 B5 (levi / peter 18:02-18:03): (the stratum record, steering on?, a refusal or None). sd is stratum_draw's
    draw for this window (None when neither flag is given). steered needs --trio-steer; unsteered turns steering off for the
    run (logged); lever (B4 x B5, peter) needs --lever-params (lever_params.load_params: the frozen line's values per platform):
    steering off (a lever window runs the base schedule plus its episodes), the params and their sha recorded in the
    stratum record, and every session gets --lever <its platform's params>; without them the window is refused."""
    if not (expect or force):
        return None, trio_steer, None
    if force:                                           # levi 19:00: stated, not drawn: {s, forced: true, k: null}
        sd = {"stratum": force, "forced": True, "k": None, "code": __import__("stratum_draw").code_sha()}
    elif sd is None or sd.get("stratum") != expect:
        return sd, trio_steer, (f"stratum mismatch: rounds_ref_n expected {expect!r}, stratum_draw gives "
                                f"{(sd or {}).get('stratum')!r} (k {(sd or {}).get('k')}); refused before trio_up")
    s = sd["stratum"]
    if s == "steered":
        return (sd, True, None) if trio_steer else (sd, False, "stratum steered needs --trio-steer; refused")
    if s == "unsteered":
        return {**sd, "steering_off_by_stratum": bool(trio_steer)}, False, None
    if lever_params is None:
        return sd, trio_steer, "stratum lever needs --lever-params (the frozen line's lever values); refused before trio_up"
    params, sha = lever_params
    return {**sd, "lever_params": params, "lever_params_sha256": sha,
            "steering_off_by_stratum": bool(trio_steer)}, False, None


NC_PIN_RX = r'"network_core==([0-9][0-9A-Za-z.]*)"'


def network_core_pin(setup_sh: Path | None = None) -> str | None:
    """The network_core version setup.sh pins (its one source; None when it pins none)."""
    import re
    try:
        m = re.search(NC_PIN_RX, (setup_sh or Path(__file__).resolve().parents[1] / "setup.sh").read_text())
    except OSError:
        return None
    return m.group(1) if m else None


def network_core_installed() -> str | None:
    """The network_core version in this interpreter's environment (the collector venv), None when not installed."""
    import importlib.metadata as md
    try:
        return md.version("network_core")
    except md.PackageNotFoundError:
        return None


def network_core_refusal(pin=None, installed=None) -> str | None:
    """levi 02:12 (brad 02:11; chaos's shared collector venv was found at 0.6.7 while setup.sh pinned 0.8.1: setup.sh is
    never re-run per worktree): a record run's network_core must be exactly setup.sh's pin, else refused before trio_up."""
    want, have = (pin or network_core_pin)(), (installed or network_core_installed)()   # looked up at call time
    if want is None:
        return "--trio-record: setup.sh pins no network_core version (network_core==<v>); refused"
    if have != want:
        return (f"--trio-record: network_core {have or 'not installed'} in this venv, setup.sh pins {want} "
                f"(pip install network_core=={want} from PyPI into the collector venv); refused before trio_up")
    return None


def arg_refusal(args, environ) -> str | None:
    """The command line's refusals (hardening stop: one pure function, tested without starting a run): the message
    orchestrate exits with, else None. The checks that need trio_slots (a pin's geometry mode) stay where it is imported,
    after the stress root is set."""
    if args.concurrent and not args.batch:
        return "--concurrent needs --batch"
    if args.trio_stress and (not args.concurrent or args.trio_record):
        return "--trio-stress needs --concurrent and never --trio-record"
    if args.uplink_cap_kbit is not None and (not args.trio_stress or args.uplink_cap_kbit <= 0):
        return "--uplink-cap-kbit needs --trio-stress and a positive rate (a diag arm, never the record)"
    if not args.batch:
        return None
    targets = {k.strip(): v for k, v in (x.split("=") for x in args.batch.split(","))}
    unknown = [k for k in targets if k not in PLATFORM_TRANSITIONS]
    if unknown:
        return f"unknown platform(s) in --batch: {unknown}"
    if args.trio_record and not args.concurrent:
        return "--trio-record needs --concurrent"
    if args.trio_record and not args.n_draw:          # restart prereg §1: the record's N is drawn per round
        return "--trio-record needs --n-draw (the record's N is drawn per round, restart prereg §1)"
    if args.n_draw and (args.slots or not args.concurrent):
        return "--n-draw needs --concurrent and no --slots (the draw fixes the composition)"
    if (args.n_support or args.n_exclude or args.n_change_sign) and not args.n_draw:
        return "--n-support / --n-exclude / --n-change-sign need --n-draw"
    if args.window_min is not None and (not args.concurrent or args.window_min <= 0):
        return "--window-min needs --concurrent and a positive length"
    if args.trio_record and args.window_min != RECORD_WINDOW_MIN:     # §10 (levi 00138): the record's window
        return f"--trio-record needs --window-min {RECORD_WINDOW_MIN:g} (restart prereg §10)"
    if args.trio_record and not args.trio_yt_fullscreen:      # §7 (levi on 2e666b0): yt's support has fullscreen
        return ("--trio-record needs --trio-yt-fullscreen (restart prereg §7: yt's geometry support includes "
                "fullscreen)")
    if args.window_min is not None and args.max_per_slot is not None:
        return "--max-per-slot does not apply inside a window (a window runs to time)"
    if args.session_range:
        if args.trio_record or not args.concurrent:
            return "--session-range is for an uncounted --concurrent run only (never the record)"
        try:
            lo, hi = (float(x) for x in args.session_range.split(","))
        except ValueError:
            return "--session-range LO,HI (minutes)"
        if not 0 < lo <= hi:
            return "--session-range LO,HI with 0 < LO <= HI"
    # levi on a2f0cac (bundle item 6): a counted run never runs with the data volume check off; preflight's
    # check_data_uuid is "not checked" when LVDC_DATA_ROOT_UUID is unset, so the record refuses that here
    if args.trio_record:                              # levi 02:12 (brad 02:11): the pinned network_core, or no record run
        _nc = network_core_refusal()
        if _nc:
            return _nc
    if args.trio_record and not (environ.get("LVDC_DATA_ROOT_UUID") or "").strip():
        return ("--trio-record needs LVDC_DATA_ROOT_UUID (the data volume's filesystem UUID; ops/env.sh / "
                "env.local.sh): preflight's data volume check must run for the record")
    if args.trio_pin and (not args.concurrent or not args.trio_pin_reason):
        return "--trio-pin needs --concurrent and --trio-pin-reason"
    if args.trio_pin and args.trio_record:        # hardening stop (levi 00:41, 7b): a pin is gate / diag only
        return "--trio-pin is gate / diag only: never with --trio-record"
    for spec in args.trio_pin:
        try:
            parse_pin(spec, args.trio_pin_reason)
        except ValueError as e:
            return str(e)
    if args.trio_yt_fullscreen and not args.concurrent:
        return "--trio-yt-fullscreen needs --concurrent"
    if args.trio_yt_fullscreen and args.trio_layout != "composite":      # t58 N-7: the keeper needs the composite
        return "--trio-yt-fullscreen needs --trio-layout composite (the fullscreen keeper runs only there)"
    if args.trio_steer_platforms is not None:
        if not args.trio_steer:
            return "--trio-steer-platforms needs --trio-steer"
        names = {x.strip() for x in args.trio_steer_platforms.split(",") if x.strip()}
        bad = names - {"youtube", "twitch", "tiktok"}
        if bad or not names:
            return f"--trio-steer-platforms: unknown or empty {sorted(bad)}"
    # collector v3 B5 (last, so every earlier refusal keeps its order)
    if args.trio_record and not args.expect_stratum:   # collector v3 B5: the counted window's stratum, checked twice
        return "--trio-record needs --expect-stratum (the window's stratum from rounds_ref_n; stratum_draw.py)"
    if args.stratum_force and args.trio_record:
        return "--stratum-force is for a dry / gate run only: never with --trio-record (a counted window's stratum is drawn)"
    if args.stratum_force and args.expect_stratum:
        return "--stratum-force and --expect-stratum exclude each other"
    if args.expect_stratum and not (args.concurrent and args.n_draw):
        return "--expect-stratum needs --concurrent and --n-draw (the stratum is drawn per n_draw window)"
    if args.stratum_force and not args.concurrent:     # levi 19:00: fixed-composition gate arms may force one without --n-draw
        return "--stratum-force needs --concurrent"
    if args.lever_params and not (args.expect_stratum or args.stratum_force):
        return "--lever-params only with --expect-stratum / --stratum-force (a lever window's values)"
    if args.steer_allow_table_era:                    # levi 19:35: dry only, never on the record root (C8)
        if args.trio_record:
            return "--steer-allow-table-era is dry only: never with --trio-record"
        import data_root as _drs
        if _drs.record_root_check(Path(environ.get("LVDC_DATA_ROOT") or _drs.REPO_DATA))["marker"]:
            return "--steer-allow-table-era is dry only: refused on a record root (its marker is present)"
    if args.replay_schedule and args.trio_record:     # v3 W-A: a replayed schedule is a dry instrument test, never record
        return "--replay-schedule is dry only: never with --trio-record"
    if args.replay_schedule and args.trio_steer:
        return "--replay-schedule needs steering off (W-A's arms differ only in the instrument flag)"
    if args.replay_step_s is not None and not args.replay_schedule:
        return "--replay-step-s only with --replay-schedule"
    if args.replay_step_s is not None and not (10.0 <= args.replay_step_s <= 600.0):
        return f"--replay-step-s {args.replay_step_s}: outside [10, 600] s"
    if args.replay_schedule and args.stratum_force == "lever":
        return "--replay-schedule excludes a lever window (the schedule is fixed)"
    if args.lever_params_sha256 and not args.lever_params:
        return "--lever-params-sha256 only with --lever-params"
    if args.lever_params:
        if not args.lever_params_sha256:              # levi 16:32: no ad-hoc lambda ever runs, the direct path included
            return "--lever-params needs --lever-params-sha256 (the sha named in the dated prereg line)"
        try:
            import lever_params as _lp_mod
            _, sha = _lp_mod.load_params(args.lever_params)
        except (OSError, ValueError) as e:
            return f"--lever-params: {e}"
        if sha != args.lever_params_sha256.strip().lower():
            return (f"--lever-params: the file's sha256 {sha[:12]} is not the pinned {args.lever_params_sha256[:12]} "
                    "(the dated prereg line's); refused")
    # t67 C8 (levi 18:45 / 18:56), last: the run's kind and its data root agree, fail closed both ways, every mode
    import data_root as _dr
    r = _dr.kind_refusal(bool(args.trio_record), Path(environ.get("LVDC_DATA_ROOT") or _dr.REPO_DATA))
    if r:
        return ("--trio-record: " if args.trio_record else "") + r
    return None


def batch_targets(spec: str) -> dict[str, int]:
    """--batch 'p=n,...' -> {platform: sessions}; a malformed count raises ValueError (a real launch dies on it)."""
    return {k.strip(): int(v) for k, v in (x.split("=") for x in spec.split(","))}


def coverage_allowed_parse(spec: str) -> tuple[dict, str | None]:
    """--coverage-allowed 'P=ID|ID,...' -> ({platform: [matrix ids]}, the refusal or None)."""
    allowed = {k.strip(): v.split("|") for k, v in (x.split("=") for x in spec.split(","))}
    from utils.shaping import PLATFORM_MATRICES
    bad = {k: v for k, v in allowed.items() if k not in PLATFORM_MATRICES or set(v) - set(PLATFORM_MATRICES[k])}
    return allowed, (f"unknown platform / matrix in --coverage-allowed: {bad}" if bad else None)


def pin_geometry_refusal(spec: str, plat: str, pin: dict, yt_fullscreen: bool) -> str | None:
    """A --trio-pin's geometry mode excluded in trio v1 (yt fullscreen only with --trio-yt-fullscreen): the refusal or None."""
    import trio_slots
    mode = (pin.get("geometry") or "").split(":")[0]
    yt_key = plat == "youtube" or plat.startswith("yt")
    if mode in trio_slots.EXCLUDED_GEOMETRIES and not (mode == "fullscreen" and yt_key and yt_fullscreen):
        return f"--trio-pin {spec!r}: geometry mode excluded in trio v1 (yt fullscreen needs --trio-yt-fullscreen)"
    return None


# The main block's refusals that read live state, left out of --check-args (each is decided at launch):
CHECK_ARGS_NOT_COVERED = (
    "--expect-stratum: stratum_draw.draw over the slots log (checked as if the draw gives the expected stratum; a "
    "mismatch or an unreadable log refuses only at launch)",
    "--n-draw: n_draw.draw over the slots log (the era's frozen support / exclusions, --n-change-sign); its parse of "
    "--n-support / --n-exclude is covered",
    "--lever-params changed between its check and its re-read (a second read at launch)",
    "everything after the parse stage: steering's coverage read, preflight, disk, offloads, slots, trio_up",
)


def _static_refusal(args, environ) -> str | None:
    """The refusals of a real launch that need no live state, in the main block's order, through the same functions."""
    r = arg_refusal(args, environ)
    if r:
        return r
    if args.coverage_allowed:
        _, r = coverage_allowed_parse(args.coverage_allowed)
        if r:
            return r
    if not args.batch:
        return None
    batch_targets(args.batch)
    for spec in args.trio_pin:
        plat, pin = parse_pin(spec, args.trio_pin_reason)
        r = pin_geometry_refusal(spec, plat, pin, bool(args.trio_yt_fullscreen))
        if r:
            return r
    if args.expect_stratum or args.stratum_force:
        lp = None
        if args.lever_params:                       # arg_refusal loaded and sha-checked it
            import lever_params as _lp_mod
            lp = _lp_mod.load_params(args.lever_params)
        sd = {"stratum": args.expect_stratum} if args.expect_stratum else None     # the draw, assumed to match
        _, steer, r = stratum_apply(sd, args.expect_stratum, args.stratum_force, bool(args.trio_steer), lp)
        if r:
            return r
    else:
        steer = bool(args.trio_steer)
    r = steer_preflight(args, environ, steer)       # levi 22:18 (B8): reads the table file, as a launch does
    if r:
        return r
    if args.concurrent:
        try:
            if args.n_draw:
                import n_draw
                n_draw.parse_support(args.n_support)
                n_draw.parse_exclude(args.n_exclude)
            else:
                import trio_slots
                trio_slots.build_slots(trio_slots.parse_composition(args.slots))
        except (ValueError, RuntimeError) as e:
            return str(e)
    return None


def check_args(argv: list[str], environ) -> str | None:
    """--check-args (ops dry_window.sh, 2026-10-25): a plan line's refusal before launch, else None. Runs the parse
    (argparse's error line, as a real launch prints it last) and every refusal that needs no live state, through the
    functions a real launch calls (_static_refusal); an exception a real launch would die on is reported as its
    traceback's last line. No browser, shaper, tc, sudo, network, session dir, slot claim or write. Reads, as a real
    launch's arg_refusal does: the --lever-params file, the data root's record marker, findmnt's volume UUID (C8), and,
    for a steered run on record, the steering table (steer_preflight, levi 22:18 B8).
    CHECK_ARGS_NOT_COVERED lists what is left to the launch."""
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        parser = build_parser()
        parser.prog = "orchestrate.py"              # a real launch's prog (argv[0]'s basename), whoever imports this
        try:
            args = parser.parse_args(argv)
        except SystemExit as e:
            if e.code in (0, None):
                return "-h / --help: prints the help, not a launch"
            lines = [x for x in err.getvalue().splitlines() if x.strip()]
            return lines[-1] if lines else f"argparse exit {e.code}"
        try:
            return _static_refusal(args, environ)
        except Exception as e:                      # noqa: BLE001  a real launch's traceback ends with this line
            return f"{type(e).__name__}: {e}"


def check_args_main(argv: list[str], environ) -> int:
    """orchestrate.py --check-args <a launch's args>: one JSON line, {"ok": true} (exit 0) or {"ok": false, "refusal"} (2)."""
    r = check_args(argv, environ)
    print(json.dumps({"ok": True} if r is None else {"ok": False, "refusal": r}), flush=True)
    return 0 if r is None else 2


if __name__ == "__main__":
    if "--check-args" in sys.argv[1:]:             # parse-only, wherever it sits (levi 14:57); check_args mirrors every
        sys.exit(check_args_main([a for a in sys.argv[1:] if a != "--check-args"], os.environ))   # static refusal below
    parser = build_parser()
    args = parser.parse_args()
    _refused = arg_refusal(args, os.environ)
    if _refused:
        sys.exit(_refused)

    DPMS_OFF = args.dpms_off
    ENTRY_QUALITY = args.entry_quality
    MATRIX_IDS = {"youtube": args.yt_matrix, "tiktok": args.tt_matrix, "twitch": args.tw_matrix}
    TT_ROOM_ROTATION_S = args.tt_room_rotation
    MATRIX_RULE = args.matrix_rule
    if args.coverage_allowed:
        COVERAGE_ALLOWED, _bad = coverage_allowed_parse(args.coverage_allowed)
        if _bad:
            sys.exit(_bad)
    if args.trio_stress:                                   # before any trio_slots import: the stress root, not data/_trio
        os.environ["LVDC_TRIO_DIR"] = "_trio_stress"
        import trio_slots
        trio_slots.TRIO_DIR, trio_slots.TRIO_NAME_FLOOR = trio_slots.STRESS_DIR, 900000
        TRIO_STRESS = True
        TRIO_STRESS_EPISODES = args.episodes
        UPLINK_CAP_KBIT = args.uplink_cap_kbit
    if args.batch:
        targets = batch_targets(args.batch)
        if args.session_range:                         # arg_refusal checked it
            SESSION_RANGE = tuple(float(x) for x in args.session_range.split(","))
        TRIO_RECORD = bool(args.trio_record)
        for spec in args.trio_pin:
            plat, pin = parse_pin(spec, args.trio_pin_reason)      # arg_refusal checked it
            _pin_ref = pin_geometry_refusal(spec, plat, pin, bool(args.trio_yt_fullscreen))
            if _pin_ref:
                sys.exit(_pin_ref)
            TRIO_PINS[plat] = pin
        TRIO_YT_FULLSCREEN = bool(args.trio_yt_fullscreen)
        REPLAY = ({"seed": args.replay_schedule, "step_s": args.replay_step_s} if args.replay_schedule else None)
        if args.steer_allow_table_era:                  # every session reads it (exp.py -> steering.steer allow_table_era)
            os.environ["LVDC_STEER_ALLOW_TABLE_ERA"] = args.steer_allow_table_era
        else:
            os.environ.pop("LVDC_STEER_ALLOW_TABLE_ERA", None)
        WINDOW_STRATUM = None
        if args.expect_stratum or args.stratum_force:      # collector v3 B5: before steering is set up and before trio_up
            import n_draw
            import record
            import stratum_draw
            import trio_slots as _ts
            _sd = None
            if args.expect_stratum:                         # a forced stratum is stated, never drawn (k null, levi 19:00)
                try:
                    _sd = stratum_draw.draw(_RUNS_DIR / _ts.TRIO_DIR / _ts.SLOTS_LOG, record.ERA, TRIO_RECORD)
                except (RuntimeError, ValueError) as e:
                    sys.exit(str(e))
            _lp = None
            if args.lever_params:                           # arg_refusal validated it; read once more for the record
                import lever_params as _lp_mod
                _lp = _lp_mod.load_params(args.lever_params)
                if _lp[1] != args.lever_params_sha256.strip().lower():   # the file changed since arg_refusal read it
                    sys.exit("--lever-params: the file changed after its check (sha mismatch); refused before trio_up")
            WINDOW_STRATUM, _steer, _ref = stratum_apply(_sd, args.expect_stratum, args.stratum_force, bool(args.trio_steer),
                                                         _lp)
            if _ref:
                sys.exit(_ref)
            if args.trio_steer and not _steer:
                print("  steering off by stratum (unsteered window; --trio-steer ignored for this run)")
            args.trio_steer = _steer
            print(f"  STRATUM: {WINDOW_STRATUM['stratum']} (k {WINDOW_STRATUM.get('k')}, block {WINDOW_STRATUM.get('block')} "
                  f"pos {WINDOW_STRATUM.get('pos')}{', forced' if WINDOW_STRATUM.get('forced') else ''}; code {WINDOW_STRATUM.get('code')})")
            os.environ["LVDC_WINDOW_STRATUM"] = json.dumps(WINDOW_STRATUM)
        else:
            os.environ.pop("LVDC_WINDOW_STRATUM", None)
        TRIO_STEER = bool(args.trio_steer)
        if args.trio_steer_platforms is not None:          # arg_refusal checked the names
            TRIO_STEER_PLATFORMS = {x.strip() for x in args.trio_steer_platforms.split(",") if x.strip()}
        _sref = steer_preflight(args, os.environ, TRIO_STEER)    # levi 22:18 (B8): before trio_up
        if _sref:
            sys.exit(_sref)
        if TRIO_STEER:                                   # prereg d32a48ce items 4 / 5; levi 20:58: per platform
            steer_balancer(TRIO_STEER_PLATFORMS)
        if args.concurrent:
            import trio_slots
            nd = None
            try:
                if args.n_draw:
                    import n_draw
                    import record
                    nd = n_draw.draw(_RUNS_DIR / trio_slots.TRIO_DIR / trio_slots.SLOTS_LOG, record.ERA, TRIO_RECORD,
                                     n_draw.parse_support(args.n_support), exclude=n_draw.parse_exclude(args.n_exclude),
                                     change_sign=args.n_change_sign)
                    comp = nd["composition"]
                    print(f"  N DRAW: round {nd['round']} ({'record' if nd['record'] else 'pilot'}, era {nd['era']}): "
                          f"N = {nd['n']}, {comp} (u {nd['u']}, support {nd['support']})")
                else:
                    comp = trio_slots.parse_composition(args.slots)
                trio_slots.build_slots(comp)
            except (ValueError, RuntimeError) as e:
                sys.exit(str(e))
            sys.exit(run_trio(targets, yt_matrix=args.yt_matrix, rotate_geometry=not args.fixed_geometry,
                              iface=args.iface, capacity_kbit=args.link_capacity_kbit, max_per_slot=args.max_per_slot,
                              layout=args.trio_layout, slots=comp, n_draw=nd, window_min=args.window_min))
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
