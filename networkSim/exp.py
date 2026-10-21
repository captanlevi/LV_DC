import sys
import os
import glob as _glob
from pathlib import Path

_VENV_DIR = Path(__file__).parent.parent / ".venv"
_VENV_PYTHON = _VENV_DIR / "bin" / "python3"
if not _VENV_PYTHON.exists():
    print("[error] .venv not found — run setup.sh first", file=sys.stderr)
    sys.exit(1)

# Re-exec with venv Python if its site-packages aren't already in sys.path.
# Checking sys.path (not sys.executable) avoids the symlink false-match where
# .venv/bin/python3 and /usr/bin/python3 resolve to the same binary.
_venv_sp = _glob.glob(str(_VENV_DIR / "lib" / "python*" / "site-packages"))
if _venv_sp and not any(p in sys.path for p in _venv_sp):
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON)] + sys.argv)

import subprocess
import time
from dataclasses import asdict
import json
import signal
import threading
import argparse
import asyncio

from utils.netStat import net_episode_generator, family_episode_generator, session_shaping
from utils.shaping import (YOUTUBE_TRANSITIONS, TWITCH_TRANSITIONS, TIKTOK_TRANSITIONS, BILIBILI_TRANSITIONS,
                           YOUTUBE_MATRICES, TIKTOK_MATRICES, TWITCH_MATRICES, initial_state_stationary,
                           get_higher_state, get_lower_state, schedule_of)
from utils.dataModels import NetLabel, NetStat
from browser import BrowserSession
import session_meta
import session_names
import compute_load
import coverage_report
import decode_path
import tw_categories
import yt_channels

from data_root import DATA_ROOT as DATA_DIR  # noqa: E402  (one setting: data_root.py)

PLATFORM_ABBREV = {
    "youtube": "yt",
    "twitch": "tw",
    "tiktok": "ti",
    "bilibili": "bi",
}


CURRENT_NET_STAT : None | NetStat = None
SYSTEM_TICK : float = 0.1

# t53 --concurrent (orchestrate.py run_trio): the session's trio slot (trio_slots.Slot) and the make variables that put
# single mode's targets on it; None / [] in single mode, whose make calls are then unchanged (test_make_single_mode.py)
SLOT = None
SLOT_VARS: list[str] = []
TRIO: dict = {}          # the trio session's meta.json concurrency record, built as the session runs
CHROME_EXTRA: list[str] = []   # trio "fb" layout: the window's position at launch; [] in single mode
trio_slots = None        # the module, imported only with --slot


def make_session_dir(platform: str, root: Path | None = None, floor: int = 0) -> Path:
    # a name is never reused, also not a removed or _failed session's (session_names: a monotonic persisted counter);
    # root / floor: a trio session's own data root (data/_trio) and counter, its names above trio_slots.TRIO_NAME_FLOOR
    return session_names.next_session_dir(root or DATA_DIR, platform, PLATFORM_ABBREV[platform], floor=floor)


def run_(cmd):
    print(f"> {cmd}")
    subprocess.run(cmd, shell=True)


def run(args):
    proc = subprocess.Popen(args, preexec_fn=os.setsid)  # NEW process group
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n🛑 Ctrl+C received — killing subprocess group")
        os.killpg(proc.pid, signal.SIGTERM)
        raise


def initialize_network() -> bool:
    if SLOT is not None:   # trio: the slot's own pieces (the shared tree is the orchestrator's)
        try:
            TRIO["slot_up"] = trio_slots.slot_up(SLOT)
            return True
        except Exception as e:
            print(e)
            TRIO["slot_up"] = {"error": repr(e)}
            return False
    try:
        run(["make", "enable"])
        return True
    except Exception as e:
        print(e)
        return False


CURRENT_DATA = Path(__file__).parent.parent / "current_data"

# v2: per-platform Chrome window size (--window-size W,H), so the player size is fixed per platform. Empty until the
# window / player decision (supervisor 2026-10-16); the player size actually obtained is recorded in meta.json.
WINDOW_SIZES: dict[str, str] = {}


EXIT_LOGIN_WALL = 3   # exp.py exit code when the session was aborted at a login wall (t37)
EXIT_STOPPED = 130    # exp.py exit code when the operator stopped the session (SIGINT / SIGTERM)
EXIT_PLAYER_FROZEN = 5   # the player froze in a stall past T (addendum N); not a failure of the batch's 5-fail rule
EXIT_ROOM_ENDED = 6      # tt: the room died mid-session (browser.py TTRoomWatch); not a failure either, the next re-picks
EXIT_PLAYER_DEAD = 7     # tt: readyState 0 with no media for PLAYER_DEAD_MAX_S (TTRoomWatch); not a failure, room re-picked
EXIT_STREAM_ENDED = 12   # yt / twitch: the tab left its live stream (browser.StreamEndedWatch); not a failure, re-picked


# v2 (LVQoE t37, rushi 2026-10-16): each session's profile is a CLONE of a per-platform template (a Chrome user-data-dir,
# logged in where the platform needs it), not an empty profile: TikTok walls logged-out viewers after ~10 s. Templates
# live at <PROFILE_TEMPLATES>/<platform>/; build one by launching Chrome once with --user-data-dir on it (as the same
# user, with the same --password-store, that exp.py launches Chrome as, or the cookies will not decrypt), logging in,
# and quitting Chrome. Never run Chrome on the template during a batch: its sha is recorded per session.
PROFILE_TEMPLATES = Path(os.environ.get("LVDC_PROFILE_TEMPLATES", str(Path(__file__).resolve().parent.parent / "profiles")))
# not copied: locks, caches (every session starts cold), crash dumps, session restore, other extensions (only ours loads)
PROFILE_CLONE_SKIP = ("Singleton*", "lockfile", "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
                      "GraphiteDawnCache", "DawnWebGPUCache", "DawnGraphiteCache", "Crashpad", "Crash Reports",
                      "BrowserMetrics*", "Sessions", "Current Session", "Current Tabs", "Last Session", "Last Tabs",
                      "Extensions", "Extension State", "component_crx_cache", "optimization_guide_model_store",
                      "CacheStorage", "ScriptCache")   # service-worker caches (yt's sw.js app shell): cold too


def dir_sha256(root: Path) -> dict:
    """sha256 over (relative path, file sha256) of every regular file, sorted; plus the file count and bytes."""
    import hashlib
    h, n, size = hashlib.sha256(), 0, 0
    for f in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        fh = hashlib.sha256(f.read_bytes()).hexdigest()
        h.update(f"{f.relative_to(root).as_posix()}\0{fh}\n".encode())
        n += 1
        size += f.stat().st_size
    return {"sha256": h.hexdigest(), "files": n, "bytes": size}


def fresh_chrome_profile(session_dir: Path, template: Path | None) -> tuple[Path, dict]:
    """A new profile per session: a clone of `template` (PROFILE_CLONE_SKIP left out), or empty when template is None
    (--empty-profile, a recorded deviation). Returns the profile and its meta.json record (template path and the sha
    of what was cloned, taken before Chrome or exp.py touch the clone)."""
    import shutil
    prof = CURRENT_DATA / "chrome_profiles" / f"{session_dir.parent.name}_{session_dir.name}_{int(time.time())}"
    if template is None:
        prof.mkdir(parents=True, exist_ok=False)
        return prof, {"path": None, "mode": "empty"}
    if not template.is_dir():
        raise ValueError(f"profile template {template} missing (build it, set LVDC_PROFILE_TEMPLATES, or pass "
                         f"--empty-profile)")
    prof.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, prof, symlinks=True, ignore=shutil.ignore_patterns(*PROFILE_CLONE_SKIP))
    return prof, {"path": str(template.resolve()), "mode": "clone", "skipped": list(PROFILE_CLONE_SKIP),
                  **dir_sha256(prof)}


def open_chrome_with_ssl_key_log(session_dir: Path, chrome_profile: Path, window_size: str | None = None,
                                 platform: str | None = None, geometry_spec: str | None = None) -> bool:
    try:
        # the key log goes to <session_dir>/ssl_keys.log (kept); the profile is the session's fresh one; the platform's
        # decode flags (decode_path.py: tt's HEVC path) and the geometry's launch args (geometry.py: a `@D` device scale
        # factor) go in as make variables
        import geometry
        run(["make", "start_chrome"] + geometry.start_chrome_make_vars(session_dir, chrome_profile, window_size, platform,
                                                                      geometry_spec, CHROME_EXTRA) + SLOT_VARS)
        return True
    except Exception as e:
        print(e)
        return False


def finalize_network() -> bool:
    if SLOT is not None:
        TRIO["slot_down"] = trio_slots.slot_down(SLOT)
        return True
    try:
        run(["make", "disable"])
        return True
    except Exception as e:
        print(e)
        return False


def initialize_pcap(session_dir: Path) -> bool:
    try:
        run(["make", "start-capture", f"SESSION_DIR={session_dir}"] + SLOT_VARS)
        return True
    except Exception as e:
        print(e)
        return False


def finalize_pcap(session_dir: Path) -> bool:
    try:
        run(["make", "stop-capture"] + SLOT_VARS)
        run(["make", "merge-capture", f"SESSION_DIR={session_dir}"] + SLOT_VARS)
        return True
    except Exception as e:
        print(e)
        return False


def kill_chrome(chrome_profile: Path | None = None) -> None:
    try:
        run(["make", "kill_chrome"] + ([f"CHROME_PROFILE={chrome_profile}"] if chrome_profile else []) + SLOT_VARS)
    except Exception as e:
        print(e)
    suffix = f"_{SLOT.tag}" if SLOT is not None else ""
    for _f in [f"chrome{suffix}.pid", f"chrome{suffix}.log"]:
        (Path(__file__).parent.parent / "current_data" / _f).unlink(missing_ok=True)


def configure_chrome_download_dir(session_dir: Path, chrome_profile: Path) -> None:
    """Write Chrome profile Preferences so all downloads go to session_dir."""
    prefs_dir = chrome_profile / "Default"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    prefs_file = prefs_dir / "Preferences"
    prefs: dict = {}
    if prefs_file.exists():
        try:
            with open(prefs_file) as f:
                prefs = json.load(f)
        except Exception:
            pass
    prefs.setdefault("download", {})
    prefs["download"]["default_directory"] = str(session_dir)
    prefs["download"]["prompt_for_download"] = False
    prefs["download"]["directory_upgrade"] = True
    # Disable session restore so old platform tabs don't come back
    prefs["session"] = {"restore_on_startup": 5}  # 5 = open NTP, no restore
    prefs["profile"] = prefs.get("profile", {})
    prefs["profile"]["exit_type"] = "Normal"
    prefs["profile"]["exited_cleanly"] = True
    # Chrome 138+ requires developer mode for --load-extension to work
    prefs.setdefault("extensions", {}).setdefault("ui", {})["developer_mode"] = True
    with open(prefs_file, "w") as f:
        json.dump(prefs, f)
    print(f"Chrome download dir set → {session_dir}")


def make_http_logs(session_dir: Path) -> bool:
    try:
        run(["make", "make_http_logs", f"SESSION_DIR={session_dir}"])
        return True
    except Exception as e:
        print(e)
        return False


def process_http_logs(session_dir: Path) -> bool:
    try:
        run(["make", "process_http_logs", f"SESSION_DIR={session_dir}"])
        return True
    except Exception as e:
        print(e)
        return False


def check_session(session_dir: Path) -> None:
    """v2: run check_session.py on the finished session (writes check.json / check.md; a FAIL moves the session
    dir to data/_failed/, archive never delete)."""
    try:
        run([sys.executable, str(Path(__file__).parent / "check_session.py"), str(session_dir), "--move-failed"])
    except Exception as e:
        print(f"check_session failed to run: {e}")


def remove_ssl_and_http_logs() -> bool:
    """v1 only. v2 keeps the TLS keys and raw decoder outputs (rushi 2026-10-16); not called."""
    try:
        run(["make", "remove_ssl_keys"])
        run(["make", "remove_http_logs"])
        return True
    except Exception as e:
        print(e)
        return False


def change_perm(session_dir: Path) -> bool:
    try:
        run(["make", "change_perm", f"SESSION_DIR={session_dir}"])
        return True
    except Exception as e:
        print(e)
        return False


def _apply_net_stat(net_stat : NetStat):
    global CURRENT_NET_STAT
    print(f"Currently shaping {net_stat}")
    CURRENT_NET_STAT = net_stat
    if net_stat.walk_step:      # restart §2 (levi on 80af314): a walk step changes only the HTB rate (make rate)
        run(["make", "rate", f"BANDWIDTH={net_stat.rate}kbit"] + SLOT_VARS)
        return
    run(
            [
                "make",
                "slow",
                f"BANDWIDTH={net_stat.rate}kbit",
                f"DELAY={net_stat.delay_ms}ms",
                f"PLR={net_stat.loss_pct}%",
            ]
            + SLOT_VARS
        )



SHAPER_OPTS: dict = {}     # v2 (t38): low-state cap and initial-state rule of the active config, set in __main__
SHAPER_GEN = net_episode_generator   # Q am. 17: a schedule family's session runs utils/netStat.family_episode_generator


async def _ossilate(
    episode_time_in_seconds: int,
    net_labels: list[NetLabel],
    stop_event : asyncio.Event,
    transition_dct: dict[str, dict[str, float]],
    forced_initial_state: str | None = None,
    max_episodes: int | None = None,
    
)-> bool:
    # returns True if the for loop ends
    deadline = None
    for net_stat in SHAPER_GEN(
        episode_length=episode_time_in_seconds,
        transition_dict=transition_dct,
        forced_initial_state=forced_initial_state,
        max_episodes=max_episodes,
        **SHAPER_OPTS,
    ):
        _apply_net_stat(net_stat= net_stat)
        ts = time.time()
        net_labels.append(    # restart §2 (c): the realised rate / delay / loss beside the nominal state and the re-draw
            NetLabel(timestamp=ts, speed=net_stat.rate, state=net_stat.state, delay_ms=net_stat.delay_ms,
                     loss_pct=net_stat.loss_pct, redraw_kbit=net_stat.redraw_kbit,
                     redraw_delay_ms=net_stat.redraw_delay_ms, redraw_loss_pct=net_stat.redraw_loss_pct,
                     walk_step=net_stat.walk_step)
        )
        # restart §2: a change every 2 s, so the changes keep the schedule's own clock (deadline + duration); an apply's
        # time (make slow) and the tick's overshoot no longer add to the session per change
        deadline = (ts if deadline is None else deadline) + net_stat.duration
        while time.time() < deadline:
            await asyncio.sleep(SYSTEM_TICK)
            if stop_event.is_set():
                return False
        
    return True

def _stdin_lines(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """Daemon thread: each stdin line into `queue`, then None at EOF. Not asyncio.to_thread: asyncio.run joins its
    executor threads on exit, so a read still pending when the last episode ended kept the process alive until stdin
    produced a line (chaos smoke run under nohup, t37). A daemon thread never holds up the exit. Raw os.read on fd 0,
    not sys.stdin: a daemon thread blocked inside the BufferedReader holds its lock, which can abort the interpreter at
    shutdown ("could not acquire lock for <stdin>")."""
    buf = b""
    while True:
        try:
            chunk = os.read(0, 4096)
        except Exception:
            chunk = b""
        if not chunk:
            loop.call_soon_threadsafe(queue.put_nowait, None)
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            loop.call_soon_threadsafe(queue.put_nowait, line.decode(errors="replace") + "\n")


async def _abort_watch(abort_check) -> str:
    """Resolves with the reason once abort_check() returns one (polled each second)."""
    while True:
        reason = abort_check()
        if reason:
            return reason
        await asyncio.sleep(1)


async def experiment(
    episode_time_in_seconds: int,
    net_labels: list[NetLabel],
    transition_dct: dict[str, dict[str, float]],
    forced_initial_state: str | None = None,
    max_episodes: int | None = None,
    abort_check=None,
):
    """abort_check: optional callable returning an abort reason or None (t37: the browser's login-wall flag); a
    reason stops the shaper loop like 'q' does, and is returned."""
    stop_event = asyncio.Event()

    def _make_task(initial_state: str | None) -> asyncio.Task:
        return asyncio.create_task(
            _ossilate(
                episode_time_in_seconds=episode_time_in_seconds,
                net_labels=net_labels,
                stop_event=stop_event,
                forced_initial_state=initial_state,
                max_episodes=max_episodes,
                transition_dct=transition_dct,
            )
        )

    osc_task = _make_task(forced_initial_state)
    abort_task = asyncio.create_task(_abort_watch(abort_check)) if abort_check else None

    # SIGINT / SIGTERM stop the shaper and return like 'q', so the normal finalise path runs (installed here because a
    # job started with `nohup ... &` from a script inherits SIGINT ignored, and SIGTERM's default kills without cleanup)
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()
    got_signal: list[str] = []

    def _on_signal(sig: signal.Signals) -> None:
        print(f"\n{sig.name} received — stopping the session (the normal finalise path runs)")
        got_signal.append(sig.name)
        stop_requested.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    stop_task = asyncio.create_task(stop_requested.wait())

    lines: asyncio.Queue = asyncio.Queue()
    threading.Thread(target=_stdin_lines, args=(loop, lines), daemon=True, name="stdin").start()
    print("[u=faster / d=slower / q=quit]")

    interactive = True   # False once stdin hits EOF without a terminal (nohup / batch): no prompt, run to the end
    input_task = None
    try:
        while True:
            if interactive and input_task is None:
                input_task = asyncio.create_task(lines.get())
            waits = {osc_task, stop_task} | ({input_task} if input_task else set()) | ({abort_task} if abort_task else set())
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)

            if abort_task in done:
                print(f"\nAborting the session: {abort_task.result()}")
                stop_event.set()
                await osc_task
                return abort_task.result()

            if stop_task in done:
                stop_event.set()
                await osc_task
                return f"signal:{got_signal[0]}" if got_signal else "signal"

            if osc_task in done:
                break   # max_episodes reached: the session ends here, whatever stdin is doing

            line = input_task.result()
            input_task = None
            if line is None:
                if not sys.stdin.isatty():
                    # no terminal (stdin is /dev/null or a closed pipe): EOF is not a quit; the session runs on
                    print("\n[stdin closed, no terminal: keyboard control off; the session runs to its episode count]")
                    interactive = False
                    continue
                # stdin closed (Ctrl+D)
                stop_event.set()
                await osc_task
                break
            user_input = line.strip().lower()

            if user_input == "q":
                stop_event.set()
                await osc_task
                break
            elif user_input in ("u", "d"):
                stop_event.set()
                await osc_task
                stop_event = asyncio.Event()
                curr = CURRENT_NET_STAT.state if CURRENT_NET_STAT is not None else None
                if curr is not None:
                    next_s = get_higher_state(curr) if user_input == "u" else get_lower_state(curr)
                else:
                    next_s = None
                print(f"  → jumping to state {next_s}")
                osc_task = _make_task(next_s)
            # unrecognized input: re-enter the wait
    except asyncio.CancelledError:
        stop_event.set()
        osc_task.cancel()
        raise
    finally:
        for t in (abort_task, stop_task, input_task):
            if t:
                t.cancel()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                pass
    return None


def _room_rx_source() -> str:
    """t58 L-1: browser.room_rx_source (exp.py's `browser` is the session's Browser, not the module)."""
    import browser as _b
    return _b.room_rx_source()


def save_json(path: str | Path, data: list[NetLabel]):
    save_data = [asdict(obj) for obj in data]
    with open(path, "w") as f:
        for item in save_data:
            json.dump(item, f)
            f.write("\n")


if __name__ == "__main__":

    platform_choices = ["youtube", "tiktok", "twitch", "bilibili"]
    from utils.shaping import state_ids
    all_states = state_ids()          # w9 (levi 02:41): every SCENARIOS state, 11 (TW-H) included

    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=platform_choices)
    parser.add_argument(
        "--initial-state",
        choices=all_states,
        default=None,
        help="Force the first episode to start at this state (overrides weighted-random).",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        default=None,
        help="Stop after this many state transitions. Default: run until Ctrl+C.",
    )
    parser.add_argument(
        "--session-minutes",
        type=float,
        default=None,
        help="Restart prereg §4 (orchestrate's trio draw, U(10, 40)): the session's length in minutes, which a "
             "@slow family's episode length divides (session / 3); --episodes is orchestrate's scaled count. Default: "
             "the platform's base length (coverage_report.SESSION_EPISODES).",
    )
    parser.add_argument(
        "--fast-episode-s",
        type=float,
        default=None,
        help="Restart prereg §2b (orchestrate's draw, log-U(12, 48) s): an @fast matrix's episode length for this "
             "session (its re-draw D/5-D/2). Only with an @fast matrix.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-navigate to a live stream and handle ads (default: manual — you navigate yourself).",
    )
    parser.add_argument(
        "--tt-matrix",
        choices=sorted(TIKTOK_MATRICES),
        default="TT-M",
        help="tt shaper config (prereg addendum M): TT-M (cap 4, stationary start) from the 2026-10-17 checkpoint; "
             "current = before it.",
    )
    parser.add_argument(
        "--tw-matrix",
        choices=sorted(TWITCH_MATRICES),
        default="TW-M",
        help="twitch shaper config (prereg addendum M): TW-M (cap 2, stationary start); TW-L (Addendum Q amendment 3: "
             "low-state, cap 2, stationary start); current = before M.",
    )
    parser.add_argument(
        "--tt-room-rotation",
        type=float,
        default=None,
        metavar="SECONDS",
        help="tt only (addendum M pre-declared fallback): move to a new room every SECONDS (300) within the session; "
             "rooms and times recorded in meta.json room_rotation.",
    )
    parser.add_argument(
        "--yt-matrix",
        choices=sorted(YOUTUBE_MATRICES),
        default="A",
        help="yt shaper config (t38): A = batch default, B = pre-declared fallback, current = pre-t38 (old data), "
             "H = yt-H (Q am. 15 (6): the Q_1080p target only, cap 2, stationary start). "
             "A / B use a low-state cap of 6 and a stationary-distribution start; ignored on other platforms.",
    )
    parser.add_argument(
        "--channel-url",
        default=None,
        help="yt: the channel to collect (orchestrate.py's rotation passes it); first in the navigation list.",
    )
    parser.add_argument(
        "--tw-category",
        default=None,
        help="twitch: the category to collect (tw_categories.py, addendum O; orchestrate.py's rotation passes it): its "
             "directory's first live card; category and content type recorded in meta.json.",
    )
    parser.add_argument(
        "--tw-channel",
        default=None,
        help="twitch esports (addendum O, levi): the live league channel the rotation picked (meta.json tw_channel).",
    )
    parser.add_argument(
        "--tw-low-latency",
        choices=("on", "off"),
        default=None,
        help="twitch (restart §8): the player's low-latency setting, set before playback (tw_latency.py; orchestrate's "
             "per-session draw passes it), read back once, recorded in meta.json tw_low_latency; never aborts. "
             "Default: the profile's own setting, untouched.",
    )
    parser.add_argument(
        "--channel-handle",
        default=None,
        help="yt: the channel's handle from yt_channels.py (meta.json; the rotation counts it).",
    )
    parser.add_argument(
        "--channel-type",
        default=None,
        help="yt: the channel's content type from the channel list (recorded in meta.json, never inferred).",
    )
    parser.add_argument(
        "--geometry",
        default=None,
        help="Window / player geometry for this session, 'player:WxH' or 'window:WxH' (orchestrate.py's rotation, "
             "geometry.py); default: the platform's pinned player size. Recorded in meta.json.",
    )
    parser.add_argument(
        "--entry-quality",
        action="store_true",
        help="Q am. 1 / am. 10 (yt / twitch, default off): at the unshaped entry pick one tier from the player's menu "
             "by the coverage snapshot's rule, restore Auto and verify it from state (entry_quality.py); meta.json entry_quality.",
    )
    parser.add_argument(
        "--dpms-off",
        action="store_true",
        help="After the browser start (geometry pin, which wakes the monitor), run `xset dpms force off` (chaos "
             "display; opt-in, recorded in meta.json). Rendering, ticks and screenshots do not depend on the display.",
    )
    parser.add_argument(
        "--profile-template",
        type=Path,
        default=None,
        help="Chrome profile to clone for this session (default: $LVDC_PROFILE_TEMPLATES/<platform>, i.e. "
             "<repo>/profiles/<platform>). Its path and sha go to meta.json.",
    )
    parser.add_argument(
        "--empty-profile",
        action="store_true",
        help="Start from an empty profile instead of the template (a deviation, recorded in meta.json).",
    )
    parser.add_argument(
        "--hold-state",
        default=None,
        help="t56 decode_stress only (the session's root must be data/_trio_stress): hold this shaper state for the whole "
             "session (initial state, every transition back to it); recorded in meta.json hold_state.",
    )
    parser.add_argument(
        "--slot",
        default=None,
        help="t53 trio only (orchestrate.py --concurrent sets it): run in this slot, a tag of LVDC_TRIO_SLOTS' composition (trio_slots.py: its "
             "cgroup, mark, IFBs, egress class, CDP port, staged capture); meta.json concurrency. Requires --auto.",
    )
    parser.add_argument(
        "--link-capacity-kbit",
        type=float,
        default=None,
        help="--slot: the measured multi-stream goodput (gate 1 dry run); recorded, and seconds whose summed slot "
             "rates exceed it are flagged (L-A2).",
    )
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=None,
        help="Custom output directory (default: data/<platform>/<abbrev>_N/).",
    )

    args = parser.parse_args()
    platform = args.platform
    if args.slot:
        import trio_slots
        err = trio_slots.slot_arg_error(args.slot, platform, args.auto)   # t56: a tag of LVDC_TRIO_SLOTS' composition
        if err:
            parser.error(err)
        SLOT = trio_slots.slot_of(args.slot)
        import browser as _browser_mod          # every CDP call of browser.py and session_meta goes to the slot's port
        _browser_mod.CDP_BASE = session_meta.CDP_BASE = f"http://localhost:{SLOT.cdp_port}"
        # t58 L-1: tt's idle rule and the shaped-bytes record read this slot's own ingress IFB, never ifb0 / every iface
        _browser_mod.SHAPED_IFACE, _browser_mod.RX_OWN_IFB = SLOT.ifb_in, True
        LAYOUT = trio_slots.layout_of(SLOT, os.environ)  # design §4: the "fb" layout puts each slot at its own origin
        if LAYOUT["layout"] == "fb":
            _browser_mod.WINDOW_ORIGIN, _browser_mod.X_SCREEN_PX = tuple(LAYOUT["origin"]), tuple(LAYOUT["x_screen_px"])
            CHROME_EXTRA[:] = [f"--window-position={LAYOUT['origin'][0]},{LAYOUT['origin'][1]}"]
    signal.signal(signal.SIGINT, signal.default_int_handler)

    def _term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)

    transition_map = {
        "youtube": YOUTUBE_TRANSITIONS,
        "tiktok": TIKTOK_TRANSITIONS,
        "twitch": TWITCH_TRANSITIONS,
        "bilibili": BILIBILI_TRANSITIONS,
    }
    transition = transition_map[platform]
    matrix_id, low_cap, init_rule = None, 2, "linear"
    chosen = {"youtube": (YOUTUBE_MATRICES, args.yt_matrix), "tiktok": (TIKTOK_MATRICES, args.tt_matrix),
              "twitch": (TWITCH_MATRICES, args.tw_matrix)}.get(platform)
    if chosen:
        matrix_id = chosen[1]
        transition, low_cap, init_rule = chosen[0][matrix_id]
    SHAPER_OPTS.update(max_consecutive_low=low_cap,
                       initial_state_fn=initial_state_stationary if init_rule == "stationary" else None)
    if args.hold_state is not None:      # t56 decode_stress: one state held, never in a dataset root
        import trio_slots as _ts
        if not args.slot or _ts.TRIO_DIR != _ts.STRESS_DIR:
            sys.exit("--hold-state is decode_stress only: it needs --slot and LVDC_TRIO_DIR=_trio_stress")
        from utils.shaping import hold_schedule
        try:
            _hold = hold_schedule(args.hold_state)
        except ValueError as e:
            sys.exit(str(e))
        transition, matrix_id, low_cap = _hold["transition"], _hold["matrix_id"], _hold["low_cap"]
        args.initial_state = args.hold_state
        SHAPER_OPTS.update(initial_state_fn=None, max_consecutive_low=_hold["max_consecutive_low"])
    # hardening stop (w9 follow-up): a start state outside the session's matrix would crash the shaper at its first
    # transition (next_state reads the state's row); refused here, before any session dir
    from utils.shaping import start_state_refusal
    _bad = start_state_refusal(args.initial_state, transition, matrix_id)
    if _bad:
        sys.exit(_bad)
    # Q am. 17: the matrix id's schedule (base, or a "<m>@slow|fast" family: its episode length, self-loop rule, re-draw
    # and low-state cap in seconds, the base matrix's cap x 60 s); recorded in meta.json shaper
    # restart §4: a drawn session length (--session-minutes) replaces the platform's base length in @slow's division
    try:
        SCHEDULE = schedule_of(matrix_id, low_cap, args.session_minutes or coverage_report.SESSION_EPISODES.get(platform),
                               episode_s=args.fast_episode_s)
    except ValueError as e:
        sys.exit(f"--fast-episode-s: {e}")
    SESSION_LEN = None
    if args.session_minutes is not None:
        try:
            SESSION_LEN = json.loads(os.environ.get("LVDC_SESSION_LENGTH") or "{}") or {"minutes": args.session_minutes}
        except ValueError:
            SESSION_LEN = {"minutes": args.session_minutes, "error": "LVDC_SESSION_LENGTH unreadable"}
        SESSION_LEN = {**SESSION_LEN, "episodes": args.episodes,
                       "scheduled_s": round(args.episodes * SCHEDULE["episode_s"], 1) if args.episodes else None}
    if SCHEDULE["schedule"] != "base":
        SHAPER_GEN = family_episode_generator
        SHAPER_OPTS = {"initial_state_fn": SHAPER_OPTS.get("initial_state_fn"), "redraw_s": tuple(SCHEDULE["redraw_s"]),
                       "low_cap_s": SCHEDULE["low_cap_s"], "self_loops": SCHEDULE["self_loops"]}

    is_capture: bool = True

    if args.save_dir:
        session_dir = args.save_dir
        session_dir.mkdir(parents=True, exist_ok=True)
    elif not args.auto:
        session_dir = Path(__file__).parent.parent / "current_data"
        session_dir.mkdir(parents=True, exist_ok=True)
    else:
        session_dir = (make_session_dir(platform) if SLOT is None else
                       make_session_dir(platform, DATA_DIR / trio_slots.TRIO_DIR, trio_slots.TRIO_NAME_FLOOR))
    print(f"Session directory: {session_dir}")
    # restart prereg 2026-10-21 §2: the walk's generator and the delay / loss factor, seeded by the session's name; the
    # record (rules, sigma per state, factor, seeds) goes to meta.json shaper
    WALK_RNG, SHAPING_REC = session_shaping(session_dir.name)
    SHAPER_OPTS.update(walk_rng=WALK_RNG, dl_factor=SHAPING_REC["dl_factor"]["value"])
    # prereg d32a48ce (label diversity at the 10-22 tip, item 2): with LVDC_STEER_TABLE set (orchestrate --trio-steer),
    # the session's dwell weights over the shaper states are drawn now, at session start, from the last post's deficit
    # table and the base matrix only; nothing during the session feeds back. A balancer band start (--initial-state)
    # still wins; the record goes to meta.json steering
    STEERING = None
    if os.environ.get("LVDC_STEER_TABLE") is not None and args.hold_state is None:
        import steering
        base_transition = transition
        transition, steer_start, STEERING = steering.steer(platform, transition,
                                                           os.environ.get("LVDC_STEER_TABLE") or None, session_dir.name)
        STEERING["base_matrix"] = base_transition
        if steer_start is not None and not args.initial_state:
            SHAPER_OPTS.update(initial_state_fn=lambda _t, _s=steer_start: _s)
    template = None if args.empty_profile else (args.profile_template or PROFILE_TEMPLATES / platform)
    chrome_profile, profile_template = fresh_chrome_profile(session_dir, template)
    from profile_clone import template_note_of
    template_note = template_note_of(template)
    configure_chrome_download_dir(session_dir, chrome_profile)
    session_meta.start(session_dir, platform, transition, episode_s=SCHEDULE["episode_s"], argv=sys.argv,
                       chrome_profile=chrome_profile, schedule={**SCHEDULE, "episodes": args.episodes,
                                                                **({"session_length": SESSION_LEN} if SESSION_LEN else {})},
                       matrix_id=matrix_id, low_state_cap=low_cap, initial_state_rule=init_rule,
                       forced_initial_state=args.initial_state, channel_url_requested=args.channel_url,
                       channel_type=args.channel_type, channel_handle=args.channel_handle,
                       profile_template=profile_template, shaping=SHAPING_REC)
    if args.hold_state is not None:
        session_meta.write(session_dir, hold_state=args.hold_state)
    if STEERING is not None:
        session_meta.write(session_dir, steering=STEERING)
    import steering as _steering           # levi 16:43 / 20:58 / 00:41: a session that is not steered says why, never silent
    _fb = _steering.fallback_of(os.environ, args.hold_state, STEERING)
    if _fb is not None:
        session_meta.write(session_dir, steering_fallback=_fb)
    if SLOT is None:
        session_meta.write(session_dir, concurrency={"mode": "single"})
    else:
        # the session's own mark epoch (its name's number), so the slot's previous session's conntrack marks miss it
        SLOT = SLOT.with_epoch(int(session_dir.name.rsplit("_", 1)[1]))
        staging = trio_slots.staging_dir(session_dir)
        staging.mkdir(parents=True, exist_ok=True)
        SLOT_VARS[:] = trio_slots.make_vars(SLOT, staging)
        slots_log = DATA_DIR / trio_slots.TRIO_DIR / trio_slots.SLOTS_LOG
        import browser as _browser_mod
        TRIO.update(trio_slots.concurrency_record(
            SLOT, staging=str(staging), link_capacity_kbit=args.link_capacity_kbit, shot_source=_browser_mod.SHOT_SOURCE,
            layout=LAYOUT,
            trio_run=os.environ.get("LVDC_TRIO_RUN"), slots_log=str(slots_log)))
        session_meta.write(session_dir, concurrency=TRIO)
        os.environ["LVDC_SLOT_TAG"] = SLOT.tag            # t56: browser.publish_stream / sibling_streams
        trio_slots.slots_log_append(slots_log, {"event": "start", "slot": SLOT.tag, "session": session_dir.name,
                                                "platform": platform, "t": time.time(), "pid": os.getpid(),
                                                "run": os.environ.get("LVDC_TRIO_RUN")})
    if template_note:   # the template's rebuild note (e.g. tt's HEVC template): the regime boundary in the session record
        session_meta.write(session_dir, template_note=template_note)
    if os.environ.get("LVDC_PREFLIGHT_JSON"):   # orchestrate's per-session preflight (preflight.py)
        try:
            session_meta.write(session_dir, preflight=json.loads(Path(os.environ["LVDC_PREFLIGHT_JSON"]).read_text()))
        except Exception as e:
            session_meta.write(session_dir, preflight={"error": repr(e)})
    channel_pick_start: dict = {}
    if os.environ.get("LVDC_CHANNEL_PICK"):  # the yt channel draw / the twitch category draw (its key "category"), counts, seeded tie
        try:
            channel_pick_start = json.loads(os.environ["LVDC_CHANNEL_PICK"])
        except Exception as e:
            channel_pick_start = {"error": repr(e)}        # kept through the end write's merge
        session_meta.write(session_dir, channel_pick=channel_pick_start)
    for env_key, meta_key in (("LVDC_GEOMETRY_DRAW", "geometry_draw"), ("LVDC_TW_LOW_LATENCY", "tw_low_latency")):
        if os.environ.get(env_key):          # restart §7 / §8: orchestrate's per-session draws (seed, support, drawn)
            try:
                session_meta.write(session_dir, **{meta_key: json.loads(os.environ[env_key])})
            except Exception as e:
                session_meta.write(session_dir, **{meta_key: {"error": repr(e)}})
    if os.environ.get("LVDC_PICK_PINNED"):   # a recorded t53 round pin (levi 00261: allowed steering if recorded)
        try:
            session_meta.write(session_dir, pick_pinned=json.loads(os.environ["LVDC_PICK_PINNED"]))
        except Exception as e:
            session_meta.write(session_dir, pick_pinned={"error": repr(e)})
    if os.environ.get("LVDC_COVERAGE_JSON"):   # orchestrate's coverage-steered matrix (prereg Addendum Q)
        try:
            session_meta.write(session_dir, coverage=coverage_report.meta_record(
                json.loads(Path(os.environ["LVDC_COVERAGE_JSON"]).read_text()), platform, matrix_id, args.initial_state))
        except Exception as e:
            session_meta.write(session_dir, coverage={"error": repr(e)})
    if platform == "youtube":   # Addendum P: every list change is recorded in the session's channel record
        session_meta.write(session_dir, channel_list=yt_channels.list_record(DATA_DIR, args.channel_handle))
    # t55 (rushi 2026-10-19: GRO off from the first counted round): the capture interface's coalescing / segmentation
    # offloads off, read back from ethtool -k and recorded; a flag that did not take fails the session before anything
    # is shaped or captured (loudly; nothing is restored: the setting holds for the era)
    import offloads
    off = offloads.ensure_off(os.environ.get("LVDC_CAPTURE_IFACE") or offloads.IFACE_DEFAULT)
    import data_root as _data_root            # the data root as set, and the volume each session was written to
    data_root_rec = _data_root.record(DATA_DIR)
    from trio_slots import TCPDUMP_B_KIB      # every session capture's kernel buffer (= the Makefile's TCPDUMP_B)
    session_meta.write(session_dir, capture={"offloads": off, "data_root": data_root_rec,
                                             "tcpdump_buffer_kib": TCPDUMP_B_KIB})
    if not off["ok"]:
        from profile_clone import remove_profile_clone
        print(f"profile clone removed: {remove_profile_clone(CURRENT_DATA / 'chrome_profiles', chrome_profile)}")
        raise RuntimeError(f"t55: capture offloads not off on {off['iface']}: {off['after']} (calls {off['calls']})")
    # box compute load during the session (supervisor 2026-10-18): 5 s samples to compute_load.jsonl, summary in meta
    load_sampler = compute_load.Sampler(session_dir).start()

    net_labels = []
    if initialize_network() == False:
        if SLOT is not None:   # a slot can fail to come up (single mode's enable cannot): no clone with cookies left
            session_meta.write(session_dir, concurrency=TRIO)
            from profile_clone import remove_profile_clone
            print(f"profile clone removed: {remove_profile_clone(CURRENT_DATA / 'chrome_profiles', chrome_profile)}")
        raise ValueError("Cannot initialize the network for collection")
    # P3 (levi 00120, counted round 1): each capture IFB's segmentation / coalescing offloads off and read back (the
    # egress mirror clones Chrome's GSO super-skbs whole; the IFB segments them before its tap only with its own
    # features off); recorded per device; a flag that did not take fails the session loudly
    ifbs = [SLOT.ifb_in, SLOT.ifb_out] if SLOT is not None else ["ifb0", "ifb1"]
    ifb_off = {d: offloads.ensure_off(d, features=offloads.IFB_OFF) for d in ifbs}
    session_meta.write(session_dir, capture={"offloads": {**off, "ifbs": ifb_off}, "data_root": data_root_rec,
                                             "tcpdump_buffer_kib": TCPDUMP_B_KIB})
    if not all(r["ok"] for r in ifb_off.values()):
        if SLOT is not None:
            session_meta.write(session_dir, concurrency=TRIO)
        from profile_clone import remove_profile_clone
        print(f"profile clone removed: {remove_profile_clone(CURRENT_DATA / 'chrome_profiles', chrome_profile)}")
        raise ValueError(f"P3: capture IFB offloads not off: {({d: r['after'] for d, r in ifb_off.items() if not r['ok']})}")
    tc_sampler = (trio_slots.TcSampler(session_dir, SLOT, capacity_kbit=args.link_capacity_kbit).start()
                  if SLOT is not None else None)
    dns_cap = trio_slots.dns_capture_start(staging) if SLOT is not None else None   # the default class's DNS (v3 §8)

    if is_capture and initialize_pcap(session_dir) == False:
        raise ValueError("Cannot initialize pcap capture")

    if open_chrome_with_ssl_key_log(session_dir, chrome_profile, WINDOW_SIZES.get(platform), platform,
                                    args.geometry) == False:
        raise ValueError("Cannot open chrome with SSL key log enabled")
    # trio: every Chrome process stays in the slot's cgroup (Chrome moves its browser process into a systemd scope)
    sweeper = trio_slots.CgroupSweeper(SLOT, lambda: trio_slots.chrome_pid_of(SLOT)).start() if SLOT is not None else None

    browser = None
    abort_reason = None
    _display_start = None                 # set once the browser runs; display_change reads it at the end
    try:
        # inside the try: a failed start (e.g. the channel is not live) still runs the finalise path below, which stops
        # tcpdump and the shaper (they were left running before, writing into the dead session's pcap)
        if args.auto:
            first_url = args.channel_url
            if platform == "twitch" and args.tw_category:
                cat = tw_categories.by_name(args.tw_category) or {"name": args.tw_category, "type": None}
                first_url = (tw_categories.channel_url(args.tw_channel) if args.tw_channel
                             else tw_categories.url_of(cat["name"]))
                session_meta.write(session_dir, tw_category=cat["name"], tw_category_type=cat["type"],
                                   tw_category_url=first_url, tw_channel=args.tw_channel,
                                   tw_category_previous=tw_categories.last_category(session_dir.parent.parent, skip=session_dir),
                                   tw_category_counts=tw_categories.collected_counts(session_dir.parent.parent))
            eq_cfg = None
            if args.entry_quality:   # the snapshot the matrix came from (orchestrate's coverage record), else none
                try:
                    eq_cfg = {"snapshot": json.loads(Path(os.environ["LVDC_COVERAGE_JSON"]).read_text()).get("snapshot")
                              if os.environ.get("LVDC_COVERAGE_JSON") else None}
                except Exception as e:
                    eq_cfg = {"snapshot": None, "error": repr(e)}
            browser = BrowserSession(platform, session_dir=session_dir, first_url=first_url, geometry=args.geometry,
                                     room_rotation_s=args.tt_room_rotation if platform == "tiktok" else None,
                                     entry_quality=eq_cfg,
                                     tw_low_latency=args.tw_low_latency if platform == "twitch" else None)
            browser.net_labels = net_labels      # the room watcher reads the current shaper state (tt)
            browser.start()
            if SLOT is not None:   # supervisor v3: the socket owner (network service) and its cgroup, once playing
                TRIO["socket_owner_start"] = trio_slots.socket_owner(trio_slots.chrome_pid_of(SLOT), SLOT)
                # t58 (levi): the staged capture is writing, once playing; an empty capture never passes as a session
                TRIO["capture_growth"] = {**trio_slots.capture_growing(staging), "at": time.time()}
                session_meta.write(session_dir, concurrency=TRIO)
                if not TRIO["capture_growth"]["ok"]:
                    raise RuntimeError(f"capture not growing in {staging}: {TRIO['capture_growth']['sizes']}")
            # Chrome's version while it surely answers (tw_43: refused at finish); finish falls back to it
            session_meta.write(session_dir, chrome_start=session_meta.chrome_version(tries=3))
            if args.dpms_off:   # best effort: a failure is recorded, never fatal to the session
                try:
                    dpms = subprocess.run(["xset", "dpms", "force", "off"], capture_output=True, text=True, timeout=10)
                    rec = {"rc": dpms.returncode, "stderr": dpms.stderr.strip()[:200]}
                except Exception as e:
                    rec = {"rc": None, "error": repr(e)}
                session_meta.write(session_dir, dpms_forced_off={**rec, "at": time.time()})
            # decision A (levi 00271 item 4): the monitor's DPMS state and the display topology, read at the start (after
            # any forced off) and at the end; both change GPU load (reverse PRIME's copy)
            _display_start = {**session_meta.display_state(), "dpms_off_requested": bool(args.dpms_off)}
            session_meta.write(session_dir, display_start=_display_start)
        else:
            # Manual mode (default): wait for Chrome to be reachable, then hand off.
            from browser import _wait_for_cdp
            if not _wait_for_cdp(30):
                log = Path(__file__).parent.parent / "current_data" / "chrome.log"
                print(f"[error] Chrome CDP not available after 30s — check {log} for Chrome errors", file=sys.stderr)
                raise ValueError("Chrome CDP not available")
            print("\n" + "="*60)
            print(f"Chrome is open. Navigate to a {platform} live video.")
            print("Press ENTER when the video is playing to start collection.")
            print("="*60 + "\n")
            input()

        abort_reason = asyncio.run(experiment(
            episode_time_in_seconds=SCHEDULE["episode_s"],
            net_labels=net_labels,
            transition_dct=transition,
            forced_initial_state=args.initial_state,
            max_episodes=args.episodes,
            abort_check=(lambda: browser.abort_reason) if browser is not None else None,
        ))

    except KeyboardInterrupt:
        print("\nInterrupted before collection started — finalising")
        abort_reason = abort_reason or "interrupted"
    except Exception as e:
        print(f"[error] session failed: {e!r} — finalising")
        abort_reason = abort_reason or f"error: {type(e).__name__}: {e}"

    finally:
        # finalising (pcap, labels, meta, check) must not be cut short: signals now only print
        for _sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(_sig, lambda *_: print("\n[finalising the session; signal deferred until it is done]"))
        channel_url = None
        meta_extra = {"window_size_flag": WINDOW_SIZES.get(platform)}
        if browser is not None:
            channel_url = browser.channel_url
            meta_extra.update(chat_action=browser.chat_action, caption_action=browser.caption_action,
                              geometry=browser.geometry, player_pin=browser.player_pin,
                              channel_list_url=browser.list_url, abort_at=browser.abort_at,
                              video_stats=browser.video_stats, geometry_target=browser.geometry_target,
                              login_state=browser.login_state, room_rotation=browser.room_rotation,
                              tt_room_choice=browser.tt_room_choice, room_ended=browser.room_ended,
                              stream_ended=browser.stream_ended,
                              stream_read_timing=({**(browser.stream_watch.timing() if browser.stream_watch else {}),
                                                   **({"yt_ladder": browser.yt_ladder.timing()} if browser.yt_ladder
                                                      else {}),
                                                   **({"tt_codec": browser.tt_codec.timing()} if browser.tt_codec
                                                      else {})} or None),
                              yt_ladder=browser.yt_ladder.record() if browser.yt_ladder else None,
                              tt_codec=browser.tt_codec.record() if browser.tt_codec else None,
                              lowstate_hold_available=browser.lowstate_hold_available,
                              frozen_clock_hold=True if browser.platform in ("youtube", "twitch") else None,  # 032f6d31
                              player_dead_watch=browser.player_dead_watch if browser.platform == "tiktok" else None,
                              player_dead=browser.player_dead, player_dead_reentries=browser.player_dead_reentries,
                              frozen_reentries=browser.frozen_reentries, censored=browser.censored,
                              player_errors=browser.player_errors, ad_wait=browser.error_watch.exposure(),
                              self_pause_resume=browser.resumer.record(),
                              **({"channel_pick": {**channel_pick_start, **browser.channel_pick}}     # twitch: + the category draw
                                 if browser.channel_pick is not None else {}),
                              t2_skipped=browser.t2_skipped, player_ladder=browser.player_ladder,
                              shot_origin_uncalibrated=(None if browser.shot_origin is None
                                                        else not browser.shot_origin.get("accepted")),
                              rung_probes=browser.rung_probes,
                              lowest_rung=browser.lowest_rung,
                              decode_path={**decode_path.spec(platform), "gpu": browser.decode_gpu, "page": browser.decode_page,
                                           **decode_path.hevc_decision(browser.decode_gpu, browser.decode_page)},
                              tw_pick=browser.tw_pick, room_play_clicks=browser.room_play_clicks,
                              room_discontinuities=browser.room_discontinuities,
                              # t58 L-1: the bytes tt's room-end rule (b) reads (a slot's own IFB, or every interface)
                              **({"room_rx_source": _room_rx_source()} if platform == "tiktok" else {}),
                              entry_quality=browser.entry_quality)
            if os.environ.get("LVDC_GEOMETRY_DRAW"):    # restart §7: the draw (written at start) with the device px taken
                try:
                    g = browser.geometry or {}
                    dpr = g.get("device_pixel_ratio")
                    dev = lambda wh: [round(x * dpr) for x in wh] if wh and dpr else None
                    meta_extra["geometry_draw"] = {**json.loads(os.environ["LVDC_GEOMETRY_DRAW"]),
                                                   "device_pixel_ratio": dpr,
                                                   "player_device_px": dev(g.get("player_client")),
                                                   "viewport_device_px": dev(g.get("viewport"))}
                except Exception as e:
                    meta_extra["geometry_draw_error"] = repr(e)[:200]
            if browser.tw_latency is not None:          # restart §8: the draw (written at start), the apply, the read-back
                try:
                    ll_draw = json.loads(os.environ.get("LVDC_TW_LOW_LATENCY") or "{}")
                except ValueError:
                    ll_draw = {}
                meta_extra["tw_low_latency"] = {**ll_draw, **browser.tw_latency}
            if platform == "youtube" and not args.channel_handle and browser.list_url:
                # manual / fallback runs: the handle and type come from the list entry navigated to, never the data
                hit = next((c for c in yt_channels.YT_CHANNELS if yt_channels.url_of(c["handle"]) == browser.list_url), None)
                if hit:
                    meta_extra.update(channel_handle=hit["handle"], channel_type=hit["type"])
            browser.stop()
            if browser.fs_keeper is not None:           # t53 (a): fullscreen in trio, the focus keeper's record
                meta_extra["fullscreen_keeper"] = browser.fs_keeper
            meta_extra["media_decoders"] = browser.media_decoders       # decision A item 3 (every platform)
            meta_extra["display_end"] = session_meta.display_state()   # decision A item 4
            meta_extra["display_change"] = session_meta.display_change(_display_start, meta_extra["display_end"])
        else:
            # Export labels from extension storage directly
            from browser import _export_via_service_worker
            _export_via_service_worker(session_dir)
        meta_extra["abort_reason"] = (browser.abort_reason if browser is not None else None) or abort_reason
        meta_extra["compute_load"] = load_sampler.stop()
        if SLOT is not None:   # never fatal: the finalise path below must run
            try:
                TRIO["cgroup_sweeper"] = sweeper.stop() if sweeper is not None else None
                TRIO["tc"] = tc_sampler.stop()
                TRIO["socket_owner_end"] = trio_slots.socket_owner(trio_slots.chrome_pid_of(SLOT), SLOT)
                t_start = json.loads((session_dir / "meta.json").read_text())["started_at"]
                TRIO["siblings"] = trio_slots.siblings(slots_log, session_dir.name, t_start, None)
                # restart §10: the sessions running beside this one, per second, and its time-weighted mean
                TRIO["concurrent"] = trio_slots.concurrency_over(slots_log, session_dir.name, t_start, None)
            except Exception as e:
                TRIO["record_error"] = repr(e)
            meta_extra["concurrency"] = TRIO
        session_meta.finish(session_dir, channel_url, **meta_extra)   # while Chrome is still up (records its version)
        kill_chrome(chrome_profile)
        if SLOT is not None:   # trio: in-flight packets land, then the slot's filters go, then its capture stops
            time.sleep(1.0)
            TRIO["slot_detach"] = trio_slots.apply(trio_slots.slot_detach_cmds(SLOT), check=False)
            time.sleep(0.5)

        if is_capture and finalize_pcap(session_dir) == False:
            raise ValueError("Cannot finalize pcap capture")
        if finalize_network() == False:
            raise ValueError("Could not finish removing the network setup")
        if SLOT is not None:   # tcpdump's kernel drop counters; its stderr files join the session dir (staging emptied)
            try:
                import shutil as _shutil
                trio_slots.dns_capture_stop(dns_cap)
                TRIO["capture_drops"] = trio_slots.capture_drops_of(staging)
                answers, TRIO["dns_answers"] = trio_slots.dns_answers_for(
                    session_dir / "merged.pcap", [staging / "dns_default.pcap"] + trio_slots.run_dns_pcaps(
                        slots_log, DATA_DIR / trio_slots.TRIO_DIR, os.environ.get("LVDC_TRIO_RUN"), session_dir.name),
                    _shutil.which("tshark") or "tshark")
                if answers:        # as LVQoE writes it (save_dns_answers); none -> LVQoE scans the pcap, also none
                    (session_dir / "dns_answers.json").write_text(json.dumps(answers, indent=2, sort_keys=True))
                (session_dir / trio_slots.SESSION_TRIO_DIR).mkdir(exist_ok=True)   # one pcap in the session dir
                for _f in list(staging.glob("tcpdump_*.err")) + list(staging.glob("dns_default.*")):
                    _shutil.move(str(_f), str(session_dir / trio_slots.SESSION_TRIO_DIR / _f.name))
                TRIO["staging_left"] = sorted(x.name for x in staging.iterdir())
                if not TRIO["staging_left"]:
                    staging.rmdir()
            except Exception as e:
                TRIO["staging_error"] = repr(e)
            session_meta.write(session_dir, concurrency=TRIO)

        http_ok = make_http_logs(session_dir)
        if not http_ok:
            print("Could not make http logs from the pcap")
        proc_ok = process_http_logs(session_dir)
        if not proc_ok:
            print("Could not process http logs — raw JSON kept for manual inspection")

        # v2: the TLS keys (<session_dir>/ssl_keys.log) and raw decoder outputs stay in the session dir

        save_json(session_dir / "net_labels.txt", net_labels)
        if platform == "tiktok":   # supervisor 2026-10-18: the stream regime (codec, ladder) per session, from http_v2
            try:
                from tt_stream import tt_stream_regime
                session_meta.write(session_dir, tt_stream=tt_stream_regime(session_dir))
            except Exception as e:
                print(f"tt_stream failed: {e!r}")
        print(f"Run complete → {session_dir}")
        change_perm(session_dir)
        try:   # addendum O: the screenshot motion cross-check (ground truth, motion.jsonl); never fatal
            import motion
            print(f"motion: {motion.compute(session_dir)}")
        except Exception as e:
            print(f"motion failed: {e!r}")
        try:   # levi 2026-10-17: per entry, the shaper state at entry, the first shaped state after it, sustained (60 s)
            import entries
            print(f"entries: {[(x['kind'], x['shaper_at_entry']['state'], x['sustained_60s']) for x in entries.write_entries(session_dir)]}")
        except Exception as e:
            print(f"entries failed: {e!r}")
        check_session(session_dir)
        # the profile clone is ephemeral runtime state with login cookies: removed after check_session (supervisor
        # 2026-10-18; profile_clone.py); the template stays the reproducible state
        from profile_clone import remove_profile_clone
        print(f"profile clone removed: {remove_profile_clone(CURRENT_DATA / 'chrome_profiles', chrome_profile)}")
        if SLOT is not None:
            trio_slots.slots_log_append(slots_log, {"event": "end", "slot": SLOT.tag, "session": session_dir.name,
                                                    "t": time.time()})

    if abort_reason == "login_wall":
        # orchestrate.py stops the platform on this code: every further logged-out session would hit the same wall
        sys.exit(EXIT_LOGIN_WALL)
    if abort_reason and abort_reason.startswith(("signal", "interrupted")):
        sys.exit(EXIT_STOPPED)    # orchestrate.py stops the whole batch
    if abort_reason == "player_frozen":
        sys.exit(EXIT_PLAYER_FROZEN)   # orchestrate.py re-picks; raw data kept (check_session FAILs it into _failed/)
    if abort_reason == "room_ended":
        sys.exit(EXIT_ROOM_ENDED)      # orchestrate.py re-picks a room next session (the ended one is now recent)
    if abort_reason == "player_dead":
        sys.exit(EXIT_PLAYER_DEAD)     # likewise: the dead room is now recent, the next session re-picks
    if abort_reason == "stream_ended":
        sys.exit(EXIT_STREAM_ENDED)    # the broadcast ended (or the page moved channel): the next session re-picks
    if abort_reason:
        sys.exit(1)               # e.g. the channel was not live: orchestrate.py moves on to the next channel
