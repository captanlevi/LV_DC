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

from utils.netStat import net_episode_generator
from utils.shaping import (YOUTUBE_TRANSITIONS, TWITCH_TRANSITIONS, TIKTOK_TRANSITIONS, BILIBILI_TRANSITIONS,
                           YOUTUBE_MATRICES, TIKTOK_MATRICES, TWITCH_MATRICES, initial_state_stationary,
                           get_higher_state, get_lower_state)
from utils.dataModels import NetLabel, NetStat
from browser import BrowserSession
import session_meta
import compute_load
import tw_categories
import yt_channels

DATA_DIR = Path(__file__).parent.parent / "data"

PLATFORM_ABBREV = {
    "youtube": "yt",
    "twitch": "tw",
    "tiktok": "ti",
    "bilibili": "bi",
}


CURRENT_NET_STAT : None | NetStat = None
SYSTEM_TICK : float = 0.1


def make_session_dir(platform: str) -> Path:
    abbrev = PLATFORM_ABBREV[platform]
    platform_dir = DATA_DIR / platform
    platform_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    # a name is never reused, also not one whose session was archived to data/_failed (provenance stays unique)
    while (platform_dir / f"{abbrev}_{n}").exists() or (DATA_DIR / "_failed" / platform / f"{abbrev}_{n}").exists():
        n += 1
    session_dir = platform_dir / f"{abbrev}_{n}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


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


def open_chrome_with_ssl_key_log(session_dir: Path, chrome_profile: Path, window_size: str | None = None) -> bool:
    try:
        # the key log goes to <session_dir>/ssl_keys.log (kept); the profile is the session's fresh one
        run(["make", "start_chrome", f"SESSION_DIR={session_dir}", f"CHROME_PROFILE={chrome_profile}"]
            + ([f"WINDOW_SIZE={window_size}"] if window_size else []))
        return True
    except Exception as e:
        print(e)
        return False


def finalize_network() -> bool:
    try:
        run(["make", "disable"])
        return True
    except Exception as e:
        print(e)
        return False


def initialize_pcap(session_dir: Path) -> bool:
    try:
        run(["make", "start-capture", f"SESSION_DIR={session_dir}"])
        return True
    except Exception as e:
        print(e)
        return False


def finalize_pcap(session_dir: Path) -> bool:
    try:
        run(["make", "stop-capture"])
        run(["make", "merge-capture", f"SESSION_DIR={session_dir}"])
        return True
    except Exception as e:
        print(e)
        return False


def kill_chrome(chrome_profile: Path | None = None) -> None:
    try:
        run(["make", "kill_chrome"] + ([f"CHROME_PROFILE={chrome_profile}"] if chrome_profile else []))
    except Exception as e:
        print(e)
    for _f in ["chrome.pid", "chrome.log"]:
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
    run(
            [
                "make",
                "slow",
                f"BANDWIDTH={net_stat.rate}kbit",
                f"DELAY={net_stat.delay_ms}ms",
                f"PLR={net_stat.loss_pct}%",
            ]
        )



SHAPER_OPTS: dict = {}     # v2 (t38): low-state cap and initial-state rule of the active config, set in __main__


async def _ossilate(
    episode_time_in_seconds: int,
    net_labels: list[NetLabel],
    stop_event : asyncio.Event,
    transition_dct: dict[str, dict[str, float]],
    forced_initial_state: str | None = None,
    max_episodes: int | None = None,
    
)-> bool:
    # returns True if the for loop ends
    for net_stat in net_episode_generator(
        episode_length=episode_time_in_seconds,
        transition_dict=transition_dct,
        forced_initial_state=forced_initial_state,
        max_episodes=max_episodes,
        **SHAPER_OPTS,
    ):
        _apply_net_stat(net_stat= net_stat)
        ts = time.time()
        net_labels.append(
            NetLabel(timestamp=ts, speed=net_stat.rate, state=net_stat.state)
        )

        while time.time() < ts + net_stat.duration:
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


def save_json(path: str | Path, data: list[NetLabel]):
    save_data = [asdict(obj) for obj in data]
    with open(path, "w") as f:
        for item in save_data:
            json.dump(item, f)
            f.write("\n")


if __name__ == "__main__":

    platform_choices = ["youtube", "tiktok", "twitch", "bilibili"]
    all_states = [str(i) for i in range(1, 11)]

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
        help="twitch shaper config (prereg addendum M): TW-M (cap 2, stationary start); current = before it.",
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
        help="yt shaper config (t38): A = batch default, B = pre-declared fallback, current = pre-t38 (old data). "
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
        "--save-dir",
        type=Path,
        default=None,
        help="Custom output directory (default: data/<platform>/<abbrev>_N/).",
    )

    args = parser.parse_args()
    platform = args.platform
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

    is_capture: bool = True

    if args.save_dir:
        session_dir = args.save_dir
        session_dir.mkdir(parents=True, exist_ok=True)
    elif not args.auto:
        session_dir = Path(__file__).parent.parent / "current_data"
        session_dir.mkdir(parents=True, exist_ok=True)
    else:
        session_dir = make_session_dir(platform)
    print(f"Session directory: {session_dir}")
    template = None if args.empty_profile else (args.profile_template or PROFILE_TEMPLATES / platform)
    chrome_profile, profile_template = fresh_chrome_profile(session_dir, template)
    configure_chrome_download_dir(session_dir, chrome_profile)
    session_meta.start(session_dir, platform, transition, episode_s=60, argv=sys.argv, chrome_profile=chrome_profile,
                       matrix_id=matrix_id, low_state_cap=low_cap, initial_state_rule=init_rule,
                       forced_initial_state=args.initial_state, channel_url_requested=args.channel_url,
                       channel_type=args.channel_type, channel_handle=args.channel_handle,
                       profile_template=profile_template)
    if os.environ.get("LVDC_PREFLIGHT_JSON"):   # orchestrate's per-session preflight (preflight.py)
        try:
            session_meta.write(session_dir, preflight=json.loads(Path(os.environ["LVDC_PREFLIGHT_JSON"]).read_text()))
        except Exception as e:
            session_meta.write(session_dir, preflight={"error": repr(e)})
    if platform == "youtube":   # Addendum P: every list change is recorded in the session's channel record
        session_meta.write(session_dir, channel_list=yt_channels.list_record(DATA_DIR, args.channel_handle))
    # box compute load during the session (supervisor 2026-10-18): 5 s samples to compute_load.jsonl, summary in meta
    load_sampler = compute_load.Sampler(session_dir).start()

    net_labels = []
    if initialize_network() == False:
        raise ValueError("Cannot initialize the network for collection")

    if is_capture and initialize_pcap(session_dir) == False:
        raise ValueError("Cannot initialize pcap capture")

    if open_chrome_with_ssl_key_log(session_dir, chrome_profile, WINDOW_SIZES.get(platform)) == False:
        raise ValueError("Cannot open chrome with SSL key log enabled")

    browser = None
    abort_reason = None
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
            browser = BrowserSession(platform, session_dir=session_dir, first_url=first_url, geometry=args.geometry,
                                     room_rotation_s=args.tt_room_rotation if platform == "tiktok" else None)
            browser.net_labels = net_labels      # the room watcher reads the current shaper state (tt)
            browser.start()
            if args.dpms_off:   # best effort: a failure is recorded, never fatal to the session
                try:
                    dpms = subprocess.run(["xset", "dpms", "force", "off"], capture_output=True, text=True, timeout=10)
                    rec = {"rc": dpms.returncode, "stderr": dpms.stderr.strip()[:200]}
                except Exception as e:
                    rec = {"rc": None, "error": repr(e)}
                session_meta.write(session_dir, dpms_forced_off={**rec, "at": time.time()})
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
            episode_time_in_seconds=60,
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
                              player_dead=browser.player_dead, player_dead_reentries=browser.player_dead_reentries,
                              tw_pick=browser.tw_pick, room_play_clicks=browser.room_play_clicks,
                              room_discontinuities=browser.room_discontinuities)
            if platform == "youtube" and not args.channel_handle and browser.list_url:
                # manual / fallback runs: the handle and type come from the list entry navigated to, never the data
                hit = next((c for c in yt_channels.YT_CHANNELS if yt_channels.url_of(c["handle"]) == browser.list_url), None)
                if hit:
                    meta_extra.update(channel_handle=hit["handle"], channel_type=hit["type"])
            browser.stop()
        else:
            # Export labels from extension storage directly
            from browser import _export_via_service_worker
            _export_via_service_worker(session_dir)
        meta_extra["abort_reason"] = (browser.abort_reason if browser is not None else None) or abort_reason
        meta_extra["compute_load"] = load_sampler.stop()
        session_meta.finish(session_dir, channel_url, **meta_extra)   # while Chrome is still up (records its version)
        kill_chrome(chrome_profile)

        if is_capture and finalize_pcap(session_dir) == False:
            raise ValueError("Cannot finalize pcap capture")
        if finalize_network() == False:
            raise ValueError("Could not finish removing the network setup")

        http_ok = make_http_logs(session_dir)
        if not http_ok:
            print("Could not make http logs from the pcap")
        proc_ok = process_http_logs(session_dir)
        if not proc_ok:
            print("Could not process http logs — raw JSON kept for manual inspection")

        # v2: the TLS keys (<session_dir>/ssl_keys.log) and raw decoder outputs stay in the session dir

        save_json(session_dir / "net_labels.txt", net_labels)
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
    if abort_reason:
        sys.exit(1)               # e.g. the channel was not live: orchestrate.py moves on to the next channel
