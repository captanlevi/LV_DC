import subprocess
import time
from dataclasses import asdict
import json
import os
import signal
import argparse
from pathlib import Path

from utils.netStat import net_episode_generator
from utils.shaping import YOUTUBE_TRANSITIONS, TWITCH_TRANSITIONS, TIKTOK_TRANSITIONS, BILIBILI_TRANSITIONS
from utils.dataModels import NetLabel
from browser import BrowserSession

DATA_DIR = Path(__file__).parent.parent / "data"

PLATFORM_ABBREV = {
    "youtube": "yt",
    "twitch": "tw",
    "tiktok": "ti",
    "bilibili": "bi",
}


def make_session_dir(platform: str) -> Path:
    abbrev = PLATFORM_ABBREV[platform]
    platform_dir = DATA_DIR / platform
    platform_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (platform_dir / f"{abbrev}_{n}").exists():
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


def open_chrome_with_ssl_key_log() -> bool:
    try:
        run(["make", "start_chrome"])
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


def kill_chrome() -> None:
    try:
        run(["make", "kill_chrome"])
    except Exception as e:
        print(e)


def configure_chrome_download_dir(session_dir: Path) -> None:
    """Write Chrome profile Preferences so all downloads go to session_dir."""
    prefs_dir = Path(__file__).parent.parent / "current_data" / "chrome_profile" / "Default"
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


def remove_ssl_and_http_logs() -> bool:
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


def ossilate(
    episode_time_in_seconds: int,
    net_labels: list[NetLabel],
    transition_dct: dict[str, dict[str, float]],
    forced_initial_state: str | None = None,
    max_episodes: int | None = None,
):
    for net_stat in net_episode_generator(
        episode_length=episode_time_in_seconds,
        transition_dict=transition_dct,
        forced_initial_state=forced_initial_state,
        max_episodes=max_episodes,
    ):
        print(f"Currently shaping {net_stat}")
        run(
            [
                "make",
                "slow",
                f"BANDWIDTH={net_stat.rate}kbit",
                f"DELAY={net_stat.delay_ms}ms",
                f"PLR={net_stat.loss_pct}%",
            ]
        )
        ts = time.time()
        net_labels.append(
            NetLabel(timestamp=ts, speed=net_stat.rate, state=net_stat.state)
        )
        time.sleep(net_stat.duration)


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
        "--save-dir",
        type=Path,
        default=None,
        help="Custom output directory (default: data/<platform>/<abbrev>_N/).",
    )

    args = parser.parse_args()
    platform = args.platform

    transition_map = {
        "youtube": YOUTUBE_TRANSITIONS,
        "tiktok": TIKTOK_TRANSITIONS,
        "twitch": TWITCH_TRANSITIONS,
        "bilibili": BILIBILI_TRANSITIONS,
    }
    transition = transition_map[platform]

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
    configure_chrome_download_dir(session_dir)

    net_labels = []
    if initialize_network() == False:
        raise ValueError("Cannot initialize the network for collection")

    if is_capture and initialize_pcap(session_dir) == False:
        raise ValueError("Cannot initialize pcap capture")

    if open_chrome_with_ssl_key_log() == False:
        raise ValueError("Cannot open chrome with SSL key log enabled")

    if args.auto:
        browser = BrowserSession(platform, session_dir=session_dir)
        browser.start()
    else:
        # Manual mode (default): wait for Chrome to be reachable, then hand off.
        from browser import _wait_for_cdp
        if not _wait_for_cdp(30):
            raise ValueError("Chrome CDP not available")
        print("\n" + "="*60)
        print(f"Chrome is open. Navigate to a {platform} live video.")
        print("Press ENTER when the video is playing to start collection.")
        print("="*60 + "\n")
        input()
        browser = None

    try:
        ossilate(
            episode_time_in_seconds=60,
            net_labels=net_labels,
            transition_dct=transition,
            forced_initial_state=args.initial_state,
            max_episodes=args.episodes,
        )

    finally:
        if browser is not None:
            browser.stop()
        else:
            # Export labels from extension storage directly
            from browser import _export_via_service_worker
            _export_via_service_worker(session_dir)
        kill_chrome()

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

        if http_ok and proc_ok:
            run(["make", "remove_http_logs"])
        run(["make", "remove_ssl_keys"])

        save_json(session_dir / "net_labels.txt", net_labels)
        print(f"Run complete → {session_dir}")
        change_perm(session_dir)
