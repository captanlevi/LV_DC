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
from dataclasses import asdict, replace as dc_replace
import json
import shutil
import signal
import threading
import argparse
import asyncio
import random

from utils.netStat import (net_episode_generator, family_episode_generator, session_shaping, replay_generator, replay_record,
                           wrap_schedule)
from utils.shaping import (YOUTUBE_TRANSITIONS, TWITCH_TRANSITIONS, TIKTOK_TRANSITIONS, BILIBILI_TRANSITIONS,
                           YOUTUBE_MATRICES, TIKTOK_MATRICES, TWITCH_MATRICES, initial_state_stationary,
                           get_higher_state, get_lower_state, schedule_of, SCENARIOS, JOIN_SETTLE_S)
from utils.dataModels import NetLabel, NetStat
from browser import BrowserSession, FROZEN_HOLD_RULE, JOIN_CAP_S
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
LEVER = None    # v3 B4 (lever.Lever): set in __main__ with --lever; _ossilate overlays its episodes on the drawn schedule
# B9 (levi 23:21-23:22, shape before join): the shaper starts first and runs through the join. _ossilate's first apply
# sets JOIN_FIRST (before navigation); the join thread (_join, __main__) then navigates on that shaped link, its waits
# scaled to the first state (browser.join_timeout_s), and sets JOIN_PLAYING once playing. The lever overlay waits for
# JOIN_PLAYING, so the lever's t0 is the first apply after playback starts (levi 23:21). The session ends at the
# schedule's end: navigation + its length (the join counts inside it, levi 23:22). meta.json join records it.
JOIN_FIRST = threading.Event()
JOIN_PLAYING = threading.Event()
JOIN: dict = {}
JOIN_FIRST_WAIT_S = 120.0
# rushi 15:20 / levi 15:24-15:25 (kb collector.v3.join_at_fixed_mid_speed): every auto session joins at ONE fixed state J
# with fixed tc (the band's midpoints, no walk, no delay / loss factor), applied before navigation (discovery stays
# unshaped), then settles JOIN_SETTLE_S at J from join.t_playing; the schedule (W, steering, forced start) starts after
# (t_settle_end). The J row in net_labels carries phase "join": never labelled, never coverage (readers skip it).
JOIN_J = "8"                 # JOIN_SETTLE_S: utils/shaping.py (orchestrate's window allowance reads it too)
# levi / shagun 03:19 (the boundary-3 dry gap's constant net state): LVDC_DRY_HOLD_J=1 holds J for the whole session,
# exactly as the join applies it (join_phase_stat: band midpoints, fixed tc, no walk, no delay / loss factor): the join's
# one tc apply stays on the link, the schedule is never played (its length still ends the session). Dry pilots only:
# refused on a counted run (LVDC_TRIO_RECORD=1) or under a record-marker root, before any session dir. meta.json
# shaper.hold; net_labels: the join row, then one row at t_settle_end (phase None, apply "hold": no tc call, same tc).
DRY_HOLD_ENV = "LVDC_DRY_HOLD_J"
DRY_HOLD = False


JOIN_STOP_WAIT_S = 30.0


def join_meta(join: dict) -> dict:
    """meta.json join: the rule beside the join's record (JOIN)."""
    rule = ("J (rushi 15:20, levi 15:25): the fixed join state J before navigation, held through the join and settle_s "
            "after t_playing; the schedule from t_settle_end" if join.get("J") is not None else
            "B9 (levi 23:21-23:22, 23:49): the first schedule state before navigation; the join inside the session")
    return {"rule": rule + "; levi 07:48: discovery (the list page) unshaped before the first apply, in its own tab, "
                           "outside join_s", **{k: v for k, v in join.items() if k != "abort"}, "abort": join.get("abort")}


def join_final_write(write) -> bool:
    """The join thread's own meta.join write, unless after_schedule already recorded the join as unfinished (brad / levi
    00:03-00:04: a thread outliving JOIN_STOP_WAIT_S must not overwrite that record). True when written."""
    if JOIN.get("unfinished"):
        return False
    write(join_meta(JOIN))
    return True


def join_unfinished_reason(join: dict, now: float) -> str:
    """B9 D2 (levi 23:49): the schedule ended with the join still running: a join_timeout at its first state (shagun's
    prefix), over the time since navigation."""
    return f"join_timeout at state {join.get('first_state')} ({int(round(now - (join.get('t_nav') or now)))} s)"


def after_schedule(join_t, browser, abort_reason, now: float | None = None, write=None):
    """B9 D2: once the shaper returned. A join still running is stopped before teardown (browser.join_cancel; the
    thread is given JOIN_STOP_WAIT_S) and the session ends as join_timeout (it overrides what the cancelled join then
    wrote); else the join's own abort, if any. `write(meta_join)` records the final JOIN."""
    if join_t is None:
        return abort_reason
    if JOIN.get("outcome") is None:
        reason = join_unfinished_reason(JOIN, time.time() if now is None else now)
        browser.join_cancel.set()
        join_t.join(timeout=JOIN_STOP_WAIT_S)
        JOIN.update(outcome="join_timeout", unfinished=True, abort=reason)
        if write is not None:
            write(join_meta(JOIN))
        return abort_reason or reason
    join_t.join(timeout=10)
    if write is not None and JOIN.get("t_settle_end") is not None:     # J: the settle's end, after the thread's write
        write(join_meta(JOIN))
    return abort_reason or JOIN.get("abort")


def join_record(t_first_apply: float | None, t_nav: float | None, t_playing: float | None,
                scheduled_s: float | None, settle_s: float | None = None) -> dict:
    """levi 23:22: the join inside the session: join_s (navigation -> playing), the schedule seconds played while
    playing (the schedule clock runs from the first apply, just before navigation), and the planned end. At a fixed J
    (settle_s): the schedule starts settle_s after playing and is played whole."""
    out: dict = {}
    if t_nav is not None and t_playing is not None:
        out["join_s"] = round(t_playing - t_nav, 3)
    if settle_s is not None and scheduled_s is not None:
        if t_playing is not None:
            out.update(session_end_planned=round(t_playing + settle_s + scheduled_s, 3), schedule_played_s=scheduled_s)
    elif t_first_apply is not None and scheduled_s is not None:
        out["session_end_planned"] = round(t_first_apply + scheduled_s, 3)
        if t_playing is not None:
            out["schedule_played_s"] = round(max(0.0, scheduled_s - (t_playing - t_first_apply)), 3)
    return out
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


MAKE_RC: dict = {}     # codex item 2 (levi 18:15): each make target's last non-zero rc this session -> meta.json make_rc


def run(args) -> int:
    """The command's rc (codex item 2: it was dropped, so a failed `make slow` still labelled its state as applied)."""
    proc = subprocess.Popen(args, preexec_fn=os.setsid)  # NEW process group
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        print("\n🛑 Ctrl+C received — killing subprocess group")
        os.killpg(proc.pid, signal.SIGTERM)
        raise
    if rc and args[:1] == ["make"] and len(args) > 1:
        MAKE_RC[args[1]] = rc
        print(f"[error] make {args[1]} rc {rc}", flush=True)
    return rc


class ShaperApplyError(RuntimeError):
    """A tc change that did not apply (make slow / rate rc != 0): its net_labels row carries apply_rc, then the session
    ends as error (codex item 2): never a state labelled as applied that the link did not get."""


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
EXIT_PLAYER_ERROR_HELD = 13   # yt / twitch (levi 02:57): a player error begun in the low-state hold; not a failure, re-picked
EXIT_DRY_HOLD_REFUSED = 15    # levi 03:19: LVDC_DRY_HOLD_J outside a dry pilot, refused before any session dir
EXIT_FRAME_LOG_REFUSED = 14   # levi 02:52: a counted v4 run without LVQOE_FRAME_LOG=1, refused before any session dir


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
        return run(["make", "start_chrome"] + geometry.start_chrome_make_vars(
            session_dir, chrome_profile, window_size, platform, geometry_spec, CHROME_EXTRA) + SLOT_VARS) == 0
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
        return run(["make", "start-capture", f"SESSION_DIR={session_dir}"] + SLOT_VARS) == 0
    except Exception as e:
        print(e)
        return False


def finalize_pcap(session_dir: Path) -> bool:
    try:
        stop = run(["make", "stop-capture"] + SLOT_VARS)
        return run(["make", "merge-capture", f"SESSION_DIR={session_dir}"] + SLOT_VARS) == 0 == stop
    except Exception as e:
        print(e)
        return False


def startup_undo(caps=(), capture: bool = False, chrome_profile=None) -> dict:
    """codex triage (b) (levi 18:15): a startup failure after the slot came up, before the session's try, stops what it
    started (the DNS / default-TCP captures, the session capture, Chrome) and takes the slot down, as the finalise would.
    Before this the next slot_up's preclean and the round's clean-box gate caught the leftovers. Never raises."""
    done: dict = {}
    caps = [p for p in caps if p is not None]
    for name, step in ([("caps", lambda: [trio_slots.dns_capture_stop(p) for p in caps])] if caps else []) \
            + ([("capture", lambda: run(["make", "stop-capture"] + SLOT_VARS))] if capture else []) \
            + ([("chrome", lambda: kill_chrome(chrome_profile))] if chrome_profile is not None else []) \
            + [("network", finalize_network)]:
        try:
            step()
            done[name] = "ok"
        except Exception as e:
            done[name] = repr(e)
    print(f"[error] startup failed: undone {done}", flush=True)
    return done


CHROME_LOG_COPY = "chrome_stderr.log"   # levi 07:48 (3): the slot's Chrome stderr, kept per session


CHROME_LOG_MTIME_SLACK_S = 2.0     # exFAT's 2 s mtime granularity


def copy_chrome_log(session_dir: Path, slot=None, since: float | None = None) -> dict:
    """levi 07:48 (3) (WC3b r1 ti_100001: the per-slot chrome log was overwritten by the next round's Chrome): the
    slot's Chrome stderr (Makefile CHROME_LOG: current_data/chrome<_tag>.log) copied into the session after Chrome
    stopped. Never raises; the record goes to meta.json chrome_log. `since` (the launch time): a log last written
    before it is an earlier session's (kill_chrome keeps the log, so a launch refused before start_chrome's > leaves the
    old one; brad 734f961d) and is not copied, recorded stale."""
    src = Path(__file__).resolve().parent.parent / "current_data" / f"chrome{'_' + slot.tag if slot is not None else ''}.log"
    try:
        if since is not None and src.stat().st_mtime < since - CHROME_LOG_MTIME_SLACK_S:
            return {"copied_from": str(src), "stale": True, "mtime": round(src.stat().st_mtime, 3), "since": round(since, 3)}
        shutil.copyfile(src, Path(session_dir) / CHROME_LOG_COPY)
        return {"copied_from": str(src), "bytes": src.stat().st_size}
    except OSError as e:
        return {"copied_from": str(src), "error": repr(e)[:200]}


def kill_chrome(chrome_profile: Path | None = None) -> None:
    try:
        run(["make", "kill_chrome"] + ([f"CHROME_PROFILE={chrome_profile}"] if chrome_profile else []) + SLOT_VARS)
    except Exception as e:
        print(e)
    suffix = f"_{SLOT.tag}" if SLOT is not None else ""
    # the stderr log is NOT removed (levi 12:19 (f): removing it here and in the Makefile recipe made copy_chrome_log
    # find nothing on every v3 session); start_chrome's > truncates it at the next launch
    (Path(__file__).parent.parent / "current_data" / f"chrome{suffix}.pid").unlink(missing_ok=True)


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
        return run(["make", "make_http_logs", f"SESSION_DIR={session_dir}"]) == 0
    except Exception as e:
        print(e)
        return False


def process_http_logs(session_dir: Path) -> bool:
    try:
        return run(["make", "process_http_logs", f"SESSION_DIR={session_dir}"]) == 0
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


def _apply_net_stat(net_stat : NetStat) -> int:
    """make slow / make rate's rc (0 = applied)."""
    global CURRENT_NET_STAT
    print(f"Currently shaping {net_stat}")
    CURRENT_NET_STAT = net_stat
    if net_stat.walk_step:      # restart §2 (levi on 80af314): a walk step changes only the HTB rate (make rate)
        return run(["make", "rate", f"BANDWIDTH={net_stat.rate}kbit"] + SLOT_VARS)
    return run(
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
WRAP_SEED: str | None = None   # W (levi 48888ba7): the session's offset seed; None = the schedule is played as generated
SCHEDULE_WRAP: dict = {}       # W's record (meta.json shaper.schedule_wrap)


def _schedule(gen_kwargs: dict, wrap: bool = True):
    """The shaper's changes: W (levi 48888ba7) on every chain-generated session (unsteered, lever, steered: the two chain
    generators with a finite episode count), never on a replay or a held state; else the generator's own stream. An
    interactive u / d jump (wrap=False; levi 13:29) plays its new schedule as generated and leaves the session's record."""
    if not wrap:
        SCHEDULE_WRAP["interactive_restarts"] = SCHEDULE_WRAP.get("interactive_restarts", 0) + 1
        SCHEDULE_WRAP["seam_index"] = None                   # the restarted stream has no seam
        return iter(SHAPER_GEN(**gen_kwargs))
    if WRAP_SEED is None or SHAPER_GEN not in (net_episode_generator, family_episode_generator) \
            or gen_kwargs.get("max_episodes") is None:
        SCHEDULE_WRAP.update(applied=False, why="not a finite chain schedule (replay / hold / unbounded)")
        return iter(SHAPER_GEN(**gen_kwargs))
    items = list(SHAPER_GEN(**gen_kwargs))
    fam = SHAPER_GEN is family_episode_generator
    out, rec = wrap_schedule(items, random.Random(f"wrap:{WRAP_SEED}"),
                             max_consecutive_low=None if fam else SHAPER_OPTS.get("max_consecutive_low", 2),
                             low_cap_s=SHAPER_OPTS.get("low_cap_s") if fam else None,
                             walk_rng=SHAPER_OPTS.get("walk_rng"), dl_factor=SHAPER_OPTS.get("dl_factor", 1.0))
    SCHEDULE_WRAP.clear()
    # levi 13:29: the start the generator drew (forced band / steering / stationary) is the schedule's first change BEFORE
    # the rotation; under W the first APPLIED row (steering.start_applied) is a random episode's, by design
    SCHEDULE_WRAP.update(applied=True, seed=f"wrap:{WRAP_SEED}", wrap_t=None,
                         start_state_prewrap=items[0].state if items else None, **rec)
    return iter(out)


def check_apply(rc, row: NetLabel) -> None:
    """Raise once the failed apply's row is in net_labels (a stub returning None counts as applied)."""
    if rc:
        raise ShaperApplyError(f"shaper apply failed (make {row.apply} rc {rc}, state {row.state} at {row.apply_start:.3f})")


def join_phase_stat(state: str) -> NetStat:
    """J's one tc change (levi 15:24): the state's band midpoints, the same in every session."""
    sc = SCENARIOS[state]
    return NetStat(rate=(sc.rate_min + sc.rate_max) // 2, duration=0.0, delay_ms=(sc.delay_min + sc.delay_max) // 2,
                   loss_pct=round((sc.loss_pct_min + sc.loss_pct_max) / 2, 3), state=state)


async def _join_phase(net_labels: list[NetLabel], stop_event: asyncio.Event, cap_s: float) -> bool:
    """J before navigation (JOIN_FIRST), held through the join and JOIN_SETTLE_S after join.t_playing; True once the
    settle ended (t_settle_end). False on a stop (an abort / signal) or when nothing played within cap_s of the apply:
    the shaper returns and after_schedule ends an unfinished join as join_timeout (D2)."""
    js = join_phase_stat(JOIN_J)
    t_apply = time.time()
    rc = _apply_net_stat(net_stat=js)
    ts = time.time()
    net_labels.append(NetLabel(timestamp=ts, speed=js.rate, state=js.state, delay_ms=js.delay_ms, loss_pct=js.loss_pct,
                               apply_start=t_apply, apply="slow", phase="join", apply_rc=rc))
    check_apply(rc, net_labels[-1])
    JOIN.update(J=js.state, t_J_apply=round(t_apply, 3), t_first_apply=round(t_apply, 3), first_state=js.state,
                first_rate_kbit=js.rate, J_tc={"rate_kbit": js.rate, "delay_ms": js.delay_ms, "loss_pct": js.loss_pct},
                settle_s=JOIN_SETTLE_S)
    JOIN_FIRST.set()
    while not JOIN_PLAYING.is_set():
        if stop_event.is_set() or time.time() >= ts + cap_s:
            return False
        await asyncio.sleep(SYSTEM_TICK)
    end = (JOIN.get("t_playing") or time.time()) + JOIN_SETTLE_S
    while time.time() < end:
        if stop_event.is_set():
            return False
        await asyncio.sleep(SYSTEM_TICK)
    JOIN["t_settle_end"] = round(time.time(), 3)
    return True


def dry_hold_refusal(env, counted: bool, args) -> str | None:
    """LVDC_DRY_HOLD_J (levi 03:19): None when unset or honoured; else why the session is refused. Only "1"; never on a
    counted run or a record-marker root; an auto session only (the J join is auto's); never with --hold-state, --lever,
    --replay-schedule or steering (each sets its own schedule)."""
    raw = env.get(DRY_HOLD_ENV)
    if raw is None or raw == "":
        return None
    if raw != "1":
        return f"{DRY_HOLD_ENV}={raw!r}: only \"1\" (hold J the whole session)"
    if counted:
        return f"{DRY_HOLD_ENV}=1 on a counted run (LVDC_TRIO_RECORD=1 or a record-marker root): dry pilots only"
    if not getattr(args, "auto", False):
        return f"{DRY_HOLD_ENV}=1 needs --auto (the J join is the auto session's)"
    if getattr(args, "hold_state", None) is not None or getattr(args, "lever", False) or \
            getattr(args, "replay_schedule", None) or env.get("LVDC_STEER_TABLE") is not None:
        return f"{DRY_HOLD_ENV}=1 excludes --hold-state, --lever, --replay-schedule and steering"
    return None


async def _hold_j(net_labels: list[NetLabel], stop_event: asyncio.Event, hold_s: float | None) -> bool:
    """LVDC_DRY_HOLD_J (levi 03:19): J stays applied after the settle for the schedule's length (None: until a stop); no
    tc call. One net_labels row at t_settle_end (phase None, apply "hold") carries J's tc, so the held seconds are the
    session's labelled ones. True when the length ran out, False on a stop."""
    js = join_phase_stat(JOIN_J)
    t0 = time.time()
    net_labels.append(NetLabel(timestamp=t0, speed=js.rate, state=js.state, delay_ms=js.delay_ms, loss_pct=js.loss_pct,
                               apply="hold"))
    while hold_s is None or time.time() < t0 + hold_s:
        if stop_event.is_set():
            return False
        await asyncio.sleep(SYSTEM_TICK)
    return True


async def _ossilate(
    episode_time_in_seconds: int,
    net_labels: list[NetLabel],
    stop_event : asyncio.Event,
    transition_dct: dict[str, dict[str, float]],
    forced_initial_state: str | None = None,
    max_episodes: int | None = None,
    wrap: bool = True,
)-> bool:
    # returns True if the for loop ends
    if not JOIN_FIRST.is_set() and not JOIN_PLAYING.is_set() and JOIN_J is not None:    # auto mode, once per session
        if not await _join_phase(net_labels, stop_event,
                                 cap_s=episode_time_in_seconds * max_episodes if max_episodes else JOIN_CAP_S):
            return False
        if DRY_HOLD:
            return await _hold_j(net_labels, stop_event, episode_time_in_seconds * max_episodes if max_episodes else None)
    deadline = None
    for i_change, net_stat in enumerate(_schedule(dict(
        episode_length=episode_time_in_seconds,
        transition_dict=transition_dct,
        forced_initial_state=forced_initial_state,
        max_episodes=max_episodes,
        **SHAPER_OPTS,
    ), wrap=wrap)):
        t_apply = time.time()                 # v3 A9: a change lies in [apply_start, timestamp]
        seam = 1 if SCHEDULE_WRAP.get("seam_index") is not None and i_change == SCHEDULE_WRAP["seam_index"] else None
        if seam:
            SCHEDULE_WRAP["wrap_t"] = round(t_apply, 3)
        ep = (LEVER.active(t_apply) if LEVER is not None and JOIN_PLAYING.is_set()   # v3 B4; B9: t0 after playing
              else None)                                                            # inside an episode only the rate changes
        if ep is not None:
            net_stat = dc_replace(net_stat, rate=LEVER.rate_for(ep, t_apply))
        rc = _apply_net_stat(net_stat= net_stat)
        ts = time.time()
        net_labels.append(    # restart §2 (c): the realised rate / delay / loss beside the nominal state and the re-draw
            NetLabel(timestamp=ts, speed=net_stat.rate, state=net_stat.state, delay_ms=net_stat.delay_ms,
                     loss_pct=net_stat.loss_pct, redraw_kbit=net_stat.redraw_kbit,
                     redraw_delay_ms=net_stat.redraw_delay_ms, redraw_loss_pct=net_stat.redraw_loss_pct,
                     walk_step=net_stat.walk_step, apply_start=t_apply, apply="rate" if net_stat.walk_step else "slow",
                     lever=ep["id"] if ep is not None else None, seam=seam, apply_rc=rc)
        )
        check_apply(rc, net_labels[-1])
        if not JOIN_FIRST.is_set():         # B9: the session's first state is on the link before the join starts
            JOIN.update(t_first_apply=round(t_apply, 3), first_state=net_stat.state, first_rate_kbit=net_stat.rate)
            JOIN_FIRST.set()
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

    def _make_task(initial_state: str | None, wrap: bool = True) -> asyncio.Task:
        return asyncio.create_task(
            _ossilate(
                episode_time_in_seconds=episode_time_in_seconds,
                net_labels=net_labels,
                stop_event=stop_event,
                forced_initial_state=initial_state,
                max_episodes=max_episodes,
                transition_dct=transition_dct,
                wrap=wrap,
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
                osc_task.result()   # a shaper exception (ShaperApplyError) ends the session as error, never as a normal end
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
                osc_task = _make_task(next_s, wrap=False)       # levi 13:29: an interactive jump is never wrapped
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
        "--initial-state-source",
        choices=("coverage", "balance", "pin"),
        default=None,
        help="Which orchestrate rule chose --initial-state (recorded in meta.json steering.start_overridden_by).",
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
        "--replay-schedule",
        default=None,
        help="v3 W-A (levi 17:1x): replay one fixed schedule (states 10->1->10, --replay-step-s each, every draw seeded by "
             "this seed only) so every round shares it; dry only; never with --hold-state, --lever or steering.",
    )
    parser.add_argument(
        "--replay-step-s",
        type=float,
        default=None,
        help="v3 W-A: the replayed schedule's step (default utils.netStat.REPLAY_STEP_S, 79 s: a 25 min session).",
    )
    parser.add_argument(
        "--lever",
        default=None,
        help="t67 B4 (prereg b6bc9246): the STALL lever, JSON {lambda_per_s, d_lo_s, d_hi_s} (a lever window only); "
             "episodes drawn at session start from the session's name, each at 0.5 x the session's lowest rung "
             "(lever.py); meta.json lever.",
    )
    parser.add_argument(
        "--geometry-name",
        default=None,
        help="t67 B1: the drawn geometry's realised name (geometry.INSTRUCTION; --geometry is its launch instruction); "
             "recorded in meta.json geometry_name and keys the steering's yt tier cap (geometry.tier_cap).",
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
    # levi 17:56: a typo'd LVQOE_SHOT_CROP would give player-cropped shots and void A7's C5 silently: refuse it here
    if os.environ.get("LVQOE_SHOT_CROP") not in (None, "player", "viewport"):
        sys.exit(f"LVQOE_SHOT_CROP={os.environ['LVQOE_SHOT_CROP']!r}: must be player or viewport; refused")
    # levi / brad 19:51: the dry-only allow flag reaches exp.py as env; refuse it on a record run here too (a stale export
    # or a launch outside orchestrate must never let a record session steer on another era's table)
    if os.environ.get("LVDC_STEER_ALLOW_TABLE_ERA"):
        import data_root as _drx
        if os.environ.get("LVDC_TRIO_RECORD") == "1" or _drx.record_root_check(_drx.DATA_ROOT)["marker"]:
            sys.exit(f"LVDC_STEER_ALLOW_TABLE_ERA={os.environ['LVDC_STEER_ALLOW_TABLE_ERA']!r} on a record run "
                     "(LVDC_TRIO_RECORD=1 or a record-marker root): dry only; refused")
    if args.lever and args.hold_state is not None:   # B4 x B5: a held decode_stress session never levers
        sys.exit("--lever and --hold-state exclude each other (a held state is decode_stress, never a lever window)")
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

    # t67 C8 (levi 18:56 / 19:00, brad): the session's kind and where it writes agree, in every branch, before any dir is
    # made: only a counted slot run (--auto, a trio slot, LVDC_TRIO_RECORD=1) may write under the verified record root
    import data_root as _dr_c8
    _of_record = bool(args.auto and SLOT is not None and os.environ.get("LVDC_TRIO_RECORD") == "1")
    _target = (args.save_dir if args.save_dir else Path(__file__).parent.parent / "current_data" if not args.auto
               else (DATA_DIR / trio_slots.TRIO_DIR if SLOT is not None else DATA_DIR))
    _c8 = _dr_c8.path_kind_refusal(_target, _of_record)
    if _c8:
        print(f"C8 REFUSED: {_c8}")
        sys.exit(2)
    # levi 02:52 (era v4 backstop; the ops pin is the first gate): a counted run (LVDC_TRIO_RECORD=1 or a record-marker
    # root, as the allow-era refusal above) of a frame-log era refuses here, before any session dir and before the
    # browser, unless LVQOE_FRAME_LOG is exactly "1": non-zero rc (the slots log's exit event), the reason in the exp log
    import data_root as _dr_fl
    import record as _rec_fl
    from browser import frame_log_record_refusal
    _flr = frame_log_record_refusal(os.environ, os.environ.get("LVDC_TRIO_RECORD") == "1"
                                    or _dr_fl.record_root_check(_dr_fl.DATA_ROOT)["marker"], _rec_fl.ERA)
    if _flr:
        print(f"FRAME_LOG REFUSED: {_flr}", flush=True)
        sys.exit(EXIT_FRAME_LOG_REFUSED)
    _dhr = dry_hold_refusal(os.environ, os.environ.get("LVDC_TRIO_RECORD") == "1"
                            or _dr_fl.record_root_check(_dr_fl.DATA_ROOT)["marker"], args)
    if _dhr:
        print(f"DRY HOLD REFUSED: {_dhr}", flush=True)
        sys.exit(EXIT_DRY_HOLD_REFUSED)
    DRY_HOLD = os.environ.get(DRY_HOLD_ENV) == "1"
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
    WRAP_SEED = (session_dir.name if args.hold_state is None and not args.replay_schedule and not DRY_HOLD
                 else None)   # W: chain sessions
    if args.replay_schedule:              # v3 W-A: one fixed schedule for every round; the session-name draws are unused
        if args.hold_state is not None or args.lever or os.environ.get("LVDC_STEER_TABLE") is not None:
            sys.exit("--replay-schedule excludes --hold-state, --lever and steering (W-A's arms differ only in the flag)")
        import functools
        _step = args.replay_step_s or None
        SHAPER_GEN = functools.partial(replay_generator, replay_seed=args.replay_schedule,
                                       **({"step_s": _step} if _step else {}))
        SHAPING_REC = {**SHAPING_REC, "replay": replay_record(args.replay_schedule, *([_step] if _step else []))}
    # prereg d32a48ce (label diversity at the 10-22 tip, item 2): with LVDC_STEER_TABLE set (orchestrate --trio-steer),
    # the session's dwell weights over the shaper states are drawn now, at session start, from the last post's deficit
    # table and the base matrix only; nothing during the session feeds back. A balancer band start (--initial-state)
    # still wins; the record goes to meta.json steering
    STEERING = None
    if os.environ.get("LVDC_STEER_TABLE") is not None and args.hold_state is None:
        import steering
        base_transition = transition
        import geometry as _geometry     # t67 B1 (levi 19:03): no pull toward a tier the drawn geometry cannot show
        _dpr = _geometry.device_scale(args.geometry) or 1.0   # brad 19:18: the session's own DPR (the instruction's @D)
        try:
            _cap = _geometry.tier_cap(platform, args.geometry_name or args.geometry, _dpr)
        except ValueError as _e:             # levi 19:17: never uncapped
            print(f"B1 REFUSED: {_e}")
            sys.exit(2)
        import record as _record          # levi 19:35: the era start (a table of another era falls back)
        transition, steer_start, STEERING = steering.steer(platform, transition,
                                                           os.environ.get("LVDC_STEER_TABLE") or None, session_dir.name,
                                                           tier_cap=_cap, era=_record.ERA,
                                                           allow_table_era=os.environ.get("LVDC_STEER_ALLOW_TABLE_ERA") or None)
        STEERING["tier_cap_dpr"] = _dpr
        STEERING["base_matrix"] = base_transition
        # t65 (shagun 12:46, tw_100110): steering's start_state is its draw only; a --initial-state wins, named here
        STEERING["start_overridden_by"] = steering.start_override(args.initial_state, args.initial_state_source)
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
    if DRY_HOLD:                          # levi 03:19: the dry gap's constant net state
        _js = join_phase_stat(JOIN_J)
        session_meta.merge(session_dir, "shaper", hold={
            "state": JOIN_J, "tc": {"rate_kbit": _js.rate, "delay_ms": _js.delay_ms, "loss_pct": _js.loss_pct},
            "env": DRY_HOLD_ENV, "rule": "J held from the join to the session's end, one tc apply (levi 03:19)"})
    if args.geometry_name:              # t67 B1: the realised name (geometry_target stays the launch instruction)
        session_meta.write(session_dir, geometry_name=args.geometry_name)
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
        import offloads as _offloads      # v3 A16: the client address from the record (readers never infer it)
        _ca = trio_slots.client_addr(os.environ.get("LVDC_CAPTURE_IFACE") or _offloads.IFACE_DEFAULT)
        TRIO.update(client_addr=_ca["addr"], client_iface=_ca)
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
    # t83 (4): the start state's rule in every session, not only in a steered one's steering block (exp.py STEERING);
    # orchestrate's trio adds the balance draw's inputs (LVDC_START_PICK), single mode records the argv's pair
    start_pick = {"initial_state": args.initial_state, "source": args.initial_state_source}
    if os.environ.get("LVDC_START_PICK"):
        try:
            start_pick = {**json.loads(os.environ["LVDC_START_PICK"]), "argv": start_pick}
        except Exception as e:
            start_pick = {**start_pick, "error": repr(e)}
    session_meta.write(session_dir, start_pick=start_pick)
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
        startup_undo()
        raise ValueError(f"P3: capture IFB offloads not off: {({d: r['after'] for d, r in ifb_off.items() if not r['ok']})}")
    tc_sampler = (trio_slots.TcSampler(session_dir, SLOT, capacity_kbit=args.link_capacity_kbit).start()
                  if SLOT is not None else None)
    # v3 A18 (levi 19:41): the session's own shaped classes every 100 ms (base truth, both instrumentation arms)
    tc_fast = trio_slots.FastTcSampler(session_dir, SLOT).start() if SLOT is not None else None
    dns_cap = trio_slots.dns_capture_start(staging) if SLOT is not None else None   # the default class's DNS (v3 §8)
    tcp_def_caps = trio_slots.default_tcp_capture_start(staging) if SLOT is not None else []   # v3 A17: its TCP headers

    if is_capture and initialize_pcap(session_dir) == False:
        startup_undo([dns_cap, *tcp_def_caps], capture=True)
        raise ValueError("Cannot initialize pcap capture")

    _t_chrome_launch = time.time()               # copy_chrome_log's since: never an earlier session's log (brad 734f961d)
    if open_chrome_with_ssl_key_log(session_dir, chrome_profile, WINDOW_SIZES.get(platform), platform,
                                    args.geometry) == False:
        try:                                    # before the session's try: the only path that skips its teardown copy
            session_meta.write(session_dir, chrome_log=copy_chrome_log(session_dir, SLOT, since=_t_chrome_launch))
        except Exception as e:
            print(f"chrome log copy failed: {e!r}")
        startup_undo([dns_cap, *tcp_def_caps], capture=is_capture, chrome_profile=chrome_profile)
        raise ValueError("Cannot open chrome with SSL key log enabled")
    # trio: every Chrome process stays in the slot's cgroup (Chrome moves its browser process into a systemd scope)
    sweeper = trio_slots.CgroupSweeper(SLOT, lambda: trio_slots.chrome_pid_of(SLOT)).start() if SLOT is not None else None

    def _join(browser) -> None:
        """B9 (levi 23:21-23:22): after the shaper's first apply, navigate on that shaped link (waits scaled to the first
        state, browser.join_timeout_s), then the post-start records and the lever's rung read that ran after
        browser.start() before B9. Any failure ends the session through abort_check: a JoinTimeout with its own reason
        ("join_timeout at state <s> (<n> s)"), anything else as the pre-B9 "error: <type>: <msg>"."""
        try:
            if not JOIN_FIRST.wait(timeout=JOIN_FIRST_WAIT_S):
                raise RuntimeError("B9: the shaper made no first apply before the join")
            from browser import JOIN_BASE_S, join_timeout_s
            per_url = join_timeout_s(browser.platform, JOIN.get("first_rate_kbit"))
            browser.join_scale, browser.join_state = per_url / JOIN_BASE_S, JOIN.get("first_state")
            if SLOT is not None:            # D3: the slot's own ingress counter (media bytes advancing over the wait)
                _rx_path = Path(f"/sys/class/net/{SLOT.ifb_in}/statistics/rx_bytes")
                browser.join_rx = lambda: int(_rx_path.read_text())
            JOIN.update(t_nav=round(time.time(), 3), per_url_timeout_s=per_url, scale=round(browser.join_scale, 3))
            browser.start()
            JOIN["t_playing"] = round(time.time(), 3)
            JOIN_PLAYING.set()
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
            # the launched browser's argv once it is up (levi 13:44, the Cast fix's S3 reads the flag): the end read alone
            # misses a browser already gone at finish (w10 yt_100096 / ti_100090: "no browser process"); record only
            session_meta.write(session_dir, chrome_cmdline_start=session_meta.chrome_cmdline())
            if LEVER is not None and browser is not None and LEVER.episodes:
                # v3 B4 (levi 19:49 / 19:57 / 19:58): the lowest offered rung, read after playback started (browser.start
                # returns then) and fixed ONCE for every episode: the first read here on the join thread; while only the
                # fallback is known, retries every 5 s on a daemon thread until 2 s before the first onset (t0, the
                # shaper's first apply after playing (B9, levi 23:21), comes after this). The shaper path never reads the
                # player.
                _lever_deadline = time.time() + LEVER.first_onset_s - 2      # <= t0 + first onset - 2

                def _lever_read():
                    try:
                        r = browser._current_rung(0.0)
                    except Exception as e:
                        print(f"[lever] rung read failed: {e!r}"[:200], flush=True)
                        return False
                    if r and r.get("source") not in (None, "fallback"):     # the fix's time is taken after the read
                        print(f"[lever] rate fix: {LEVER.fix_rate(r, time.time(), deadline=_lever_deadline)}", flush=True)
                        return True
                    return False
                if not _lever_read():
                    def _lever_retry():
                        while time.time() < _lever_deadline and LEVER.rate is None:
                            time.sleep(5)
                            if _lever_read():
                                return
                    threading.Thread(target=_lever_retry, name="lever-rung", daemon=True).start()
            JOIN["outcome"] = "playing"
        except Exception as e:
            from browser import JoinTimeout
            jt = isinstance(e, JoinTimeout)
            msg = str(e) if jt else f"error: {type(e).__name__}: {e}"
            if not JOIN.get("unfinished"):        # after_schedule already ended it (a cancelled join's error): keep that
                JOIN.update(outcome="join_timeout" if jt else "error", detail=getattr(e, "detail", None), abort=msg)
            print(f"[join] {msg}", flush=True)
        finally:
            try:
                JOIN.update(join_record(JOIN.get("t_first_apply"), JOIN.get("t_nav"), JOIN.get("t_playing"),
                                        (args.episodes * SCHEDULE["episode_s"]) if args.episodes else None,
                                        settle_s=JOIN.get("settle_s")))
                join_final_write(lambda j: session_meta.write(session_dir, join=j))
            except Exception as e:
                print(f"[join] meta write failed: {e!r}", flush=True)

    browser = None
    tcp_state = None                             # t93 row 13: the slot's TCP state sampler (frame log only)
    abort_reason = None
    _join_t = None                       # B9: the join thread (auto mode)
    _display_start = None                 # set just before browser.start(); display_change reads it at the end
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
            if SLOT is not None and browser.frame_log and browser.frame_log.get("enabled"):   # t93 row 13, ON blocks
                _fl_block = browser.frame_log["block_s"]
                tcp_state = trio_slots.TcpStateSampler(session_dir, SLOT,
                                                       lambda: _browser_mod.frame_log_arm(_fl_block)).start()
            if args.lever:                       # v3 B4: the plan is fixed now, before playback and before the shaper starts
                import lever as _lever
                _horizon = (args.episodes or coverage_report.SESSION_EPISODES.get(platform) or 1) * SCHEDULE["episode_s"]
                LEVER = _lever.Lever(platform, session_dir.name, float(_horizon), json.loads(args.lever))
            # decision A (levi 00271 item 4): the monitor's DPMS state and the display topology, read at the start and at
            # the end; both change GPU load (reverse PRIME's copy). Read and written BEFORE browser.start() (WA3 r4 ruling,
            # 2026-10-25): start() returns only once playing, so a session that aborts in start() had no display_start and
            # A15 ("display held at session start") misread it. The --dpms-off force-off below runs after start(), so with
            # --dpms-off this is the state before the forced off (dpms_forced_off holds the force-off's own record).
            _display_start = {**session_meta.display_state(), "dpms_off_requested": bool(args.dpms_off)}
            session_meta.write(session_dir, display_start=_display_start)
            # levi 07:48: discovery UNSHAPED, before the shaper starts (experiment() below makes the first apply): the
            # list page in its own tab, closed again; a failure here ends the session before any shaping
            _tc0 = None                      # brad / levi 08:18: the slot's tree at the discovery's start, recorded
            if tc_sampler is not None:       # (no netem under the slot = unshaped), not assumed
                try:
                    _st = tc_sampler.slot_tc().get("slot_tc") or {}
                    _tc0 = {"in_netem": (_st.get("in") or {}).get("netem"), "out_netem": (_st.get("out") or {}).get("netem")}
                    _tc0["unshaped"] = not (_tc0["in_netem"] or _tc0["out_netem"])
                except Exception as e:
                    _tc0 = {"error": repr(e)[:200]}
            try:
                browser.discover()
            finally:                         # written now too: a failed discovery has no join thread to write it
                JOIN["discovery"] = {**browser.discovery, "tc_at_start": _tc0}
                session_meta.write(session_dir, join=join_meta(JOIN))
            _join_t = threading.Thread(target=_join, args=(browser,), name="join", daemon=True)
            _join_t.start()                  # B9: it waits for the shaper's first apply, then navigates
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
            JOIN_PLAYING.set()                   # manual: the user joined before the shaper starts (as before B9)
        abort_reason = asyncio.run(experiment(
            episode_time_in_seconds=SCHEDULE["episode_s"],
            net_labels=net_labels,
            transition_dct=transition,
            forced_initial_state=args.initial_state,
            max_episodes=args.episodes,
            abort_check=(lambda: browser.abort_reason or JOIN.get("abort")) if browser is not None else None,
        ))
        abort_reason = after_schedule(_join_t, browser, abort_reason,     # B9: a failed / unfinished join ends it
                                      write=lambda j: session_meta.write(session_dir, join=j))

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
            browser.resumer.stop()             # t83 (3): the press record below is final (s1 tw_100001's teardown press)
            channel_url = browser.channel_url
            meta_extra.update(chat_action=browser.chat_action, caption_action=browser.caption_action,
                              geometry=browser.geometry, player_pin=browser.player_pin,
                              channel_list_url=browser.list_url, abort_at=browser.abort_at,
                              video_stats=browser.video_stats, geometry_target=browser.geometry_target,
                              login_state=browser.login_state, room_rotation=browser.room_rotation,
                              tt_room_choice=browser.tt_room_choice, room_ended=browser.room_ended,
                              pin_check=browser.pin_check,                             # WA2 r1: landed vs given
                              stream_ended=browser.stream_ended,
                              stream_read_timing=({**(browser.stream_watch.timing() if browser.stream_watch else {}),
                                                   **({"yt_ladder": browser.yt_ladder.timing()} if browser.yt_ladder
                                                      else {}),
                                                   **({"tt_codec": browser.tt_codec.timing()} if browser.tt_codec
                                                      else {})} or None),
                              yt_ladder=browser.yt_ladder.record() if browser.yt_ladder else None,
                              tt_codec=browser.tt_codec.record() if browser.tt_codec else None,
                              tt_onboarding=(browser.tt_onboarding if browser.platform == "tiktok" else None),   # #10
                              lowstate_hold_available=browser.lowstate_hold_available,
                              frozen_clock_hold=True if browser.platform in ("youtube", "twitch") else None,  # 032f6d31
                              frozen_hold=({"rule": FROZEN_HOLD_RULE, "bypassed": browser.frozen_hold_bypassed}
                                           if browser.platform in ("youtube", "twitch") else None),     # levi 13:09
                              player_dead_watch=browser.player_dead_watch if browser.platform == "tiktok" else None,
                              player_dead=browser.player_dead, player_dead_reentries=browser.player_dead_reentries,
                              frozen_reentries=browser.frozen_reentries, censored=browser.censored,
                              player_errors=browser.player_errors, ad_wait=browser.error_watch.exposure(),
                              self_pause_resume=browser.resumer.record(),
                              instruments=browser.instruments,                         # v3 (t67; levi 18:33)
                              screen=browser.screen,                                   # v3 A8 (t67)
                              clock_offsets=browser.clock_offsets,                     # v3 A11 (t67)
                              shot_crop=browser.shot_crop,                             # v3 (levi 17:45; A7's C5)
                              shot_triggers_py=browser.shot_triggers_py,               # v3: Python-side shot triggers
                              shot_triggers_py_suppressed=browser.shot_triggers_py_suppressed,   # levi 00:53
                              lever=(LEVER.record(browser.abort_at, browser.abort_reason)      # v3 B4
                                     if LEVER is not None else None),
                              tw_ladder=(browser._master.tw_ladder()                  # v3 A4 (twitch's offer)
                                         if getattr(browser, "_master", None) is not None else None),
                              tt_room_enter=(browser._tt_room.tt_room_enter()          # item #9 (tt's played offer)
                                             if getattr(browser, "_tt_room", None) is not None else None),
                              tw_playlist_ads=({"fetches": browser._master.playlist_fetches,          # v3 A3 src 2
                                                "edges": browser._master.ad_edges,
                                                "overflow": browser._master.ad_edges_overflow}
                                               if getattr(browser, "_master", None) is not None else None),
                              tw_stream_type=(browser._master.tw_stream_type()        # record only (2026-10-25)
                                              if getattr(browser, "_master", None) is not None else None),
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
            if browser.frame_log:                       # t91: only when asked (dry); absent otherwise
                if tcp_state is not None:
                    browser.frame_log["tcp_state"] = tcp_state.stop()
                meta_extra["frame_log"] = browser.frame_log
            try:                                        # v3 A4: one offer schema for every platform (meta.json offer)
                import offer as _offer
                meta_extra["offer"] = _offer.offer_record(platform, {k: meta_extra.get(k)
                                                                     for k in ("yt_ladder", "tw_ladder", "tt_codec")})
            except Exception as e:
                meta_extra["offer"] = {"platform": platform, "missing": True, "error": repr(e)[:200]}
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
            meta_extra["drain"] = browser.drain       # v3 A12: run inside stop(), after the label export
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
                TRIO["tc_fast"] = tc_fast.stop() if tc_fast is not None else None     # v3 A18
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
        try:
            session_meta.write(session_dir, chrome_log=copy_chrome_log(session_dir, SLOT, since=_t_chrome_launch))
        except Exception as e:
            print(f"chrome log copy failed: {e!r}")
        if SLOT is not None:   # trio: in-flight packets land, then the slot's filters go, then its capture stops
            time.sleep(1.0)
            TRIO["slot_detach"] = trio_slots.apply(trio_slots.slot_detach_cmds(SLOT), check=False)
            time.sleep(0.5)

        if is_capture and finalize_pcap(session_dir) == False:   # recorded in make_rc; the finalise goes on
            print("[error] capture stop / merge failed: see meta.json make_rc", flush=True)
        if finalize_network() == False:
            raise ValueError("Could not finish removing the network setup")
        if SLOT is not None:   # tcpdump's kernel drop counters; its stderr files join the session dir (staging emptied)
            try:
                import shutil as _shutil
                trio_slots.dns_capture_stop(dns_cap)
                for _p in tcp_def_caps:
                    trio_slots.dns_capture_stop(_p)
                TRIO["default_tcp"] = {"snaplen": trio_slots.DEFAULT_TCP_SNAPLEN, "filter": trio_slots.DEFAULT_TCP_FILTER,
                                       "bytes": {f.name: f.stat().st_size for f in staging.glob("default_tcp_*.pcap")}}
                TRIO["capture_drops"] = trio_slots.capture_drops_of(staging)
                answers, TRIO["dns_answers"] = trio_slots.dns_answers_for(
                    session_dir / "merged.pcap", [staging / "dns_default.pcap"] + trio_slots.run_dns_pcaps(
                        slots_log, DATA_DIR / trio_slots.TRIO_DIR, os.environ.get("LVDC_TRIO_RUN"), session_dir.name),
                    _shutil.which("tshark") or "tshark")
                if answers:        # as LVQoE writes it (save_dns_answers); none -> LVQoE scans the pcap, also none
                    (session_dir / "dns_answers.json").write_text(json.dumps(answers, indent=2, sort_keys=True))
                (session_dir / trio_slots.SESSION_TRIO_DIR).mkdir(exist_ok=True)   # one pcap in the session dir
                for _f in (list(staging.glob("tcpdump_*.err")) + list(staging.glob("dns_default.*"))
                           + list(staging.glob("default_tcp_*"))):
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
        session_meta.merge(session_dir, "shaper", schedule_wrap=dict(SCHEDULE_WRAP) or {"applied": False,
                                                                                    "why": "the shaper never started"})
        if STEERING is not None:            # t65: the start the shaper applied (its first row), beside the draw
            session_meta.merge(session_dir, "steering", start_applied=session_meta.steering_start_applied(net_labels))
        if platform == "tiktok":   # supervisor 2026-10-18: the stream regime (codec, ladder) per session, from http_v2
            try:
                from tt_stream import tt_stream_regime
                session_meta.write(session_dir, tt_stream=tt_stream_regime(session_dir))
            except Exception as e:
                print(f"tt_stream failed: {e!r}")
        session_meta.write(session_dir, make_rc=dict(MAKE_RC))   # codex item 2: {} = every make target returned 0
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
        # levi 09:23 (F1): an export that failed or never ran leaves the extension's records only in the clone; they are
        # copied into the session dir before the clone goes (profile_clone.keep_ext_storage); never fatal. BEFORE
        # check_session, which moves a FAILed dir to _failed/ (FIN 13:30, 54ed22cd: run after it, the old path was gone,
        # FileNotFoundError, nothing kept; a failed export always FAILs). F1 block begin
        try:
            ext_export = json.loads((session_dir / "meta.json").read_text()).get("ext_export") or {"ok": False,
                                                                                                  "reason": "not_run"}
            from profile_clone import f1_ref_keep, keep_ext_storage
            if ext_export.get("ok") is not True:
                session_meta.write(session_dir, ext_export={**ext_export, "raw": keep_ext_storage(chrome_profile,
                                                                                                    session_dir)})
            elif f1_ref_keep(session_dir.name) is not None:   # t102 #7: the F1 reference keep, 1 in F1_REF_KEEP_EVERY
                ref = f1_ref_keep(session_dir.name)
                session_meta.write(session_dir, ext_export={**ext_export, "ref_keep": ref, **(
                    {"raw": keep_ext_storage(chrome_profile, session_dir)} if ref["selected"] else {})})
        except Exception as e:
            print(f"ext storage not kept: {e!r}")
        # F1 block end
        check_session(session_dir)
        # the profile clone is ephemeral runtime state with login cookies: removed after check_session (supervisor
        # 2026-10-18; profile_clone.py); the template stays the reproducible state
        from profile_clone import remove_profile_clone
        print(f"profile clone removed: {remove_profile_clone(CURRENT_DATA / 'chrome_profiles', chrome_profile)}")
        if SLOT is not None:
            trio_slots.slots_log_append(slots_log, {"event": "end", "slot": SLOT.tag, "session": session_dir.name,
                                                    "t": time.time()})

    # hardening stop (coverage audit): the exit code reads the reason meta.json records (the browser's first, else the
    # session's own), so an abort the browser set after the episodes ended exits as recorded
    abort_reason = (browser.abort_reason if browser is not None else None) or abort_reason
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
        sys.exit(EXIT_STREAM_ENDED)
    import stuck as _stuck
    if abort_reason == "player_error" and browser is not None and _stuck.player_error_held(browser.player_errors,
                                                                                            net_labels):
        sys.exit(EXIT_PLAYER_ERROR_HELD)   # our own low-state shaping explains it: no failure, no not-live mark    # the broadcast ended (or the page moved channel): the next session re-picks
    if abort_reason:
        sys.exit(1)               # e.g. the channel was not live: orchestrate.py moves on to the next channel
