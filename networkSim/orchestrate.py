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

    # Just print the current balance without collecting anything
    python orchestrate.py --platform youtube --report-only
    python orchestrate.py --all-platforms --report-only
"""
import subprocess
import sys
import argparse
from pathlib import Path

from balance import PLATFORM_TRANSITIONS, most_needed_state, balance_report
import yt_channels

_RUNS_DIR = Path(__file__).parent.parent / "data"

EXIT_LOGIN_WALL = 3   # exp.py's exit code for a session aborted at a login wall (t37)
EXIT_STOPPED = 130    # exp.py's exit code when the operator stopped the session

# collection v2 (LVQoE prereg t36 §1): episodes per session are fixed per platform, not left to the operator
EPISODES_V2 = {"youtube": 21, "tiktok": 23, "twitch": 23}


def _prompt_ready(platform: str, initial_state: str | None) -> bool:
    state_hint = f"starting at state {initial_state}" if initial_state else "weighted-random start"
    print(f"\n  Platform : {platform.upper()}")
    print(f"  Strategy : {state_hint}")
    print(f"\n  Open {platform.capitalize()} Live in Chrome and navigate to a live stream.")
    ans = input("  Press Enter when ready  (or 'q' to abort this run): ").strip().lower()
    return ans != "q"


def _run_collection(platform: str, initial_state: str | None, episodes: int, auto: bool = False,
                    yt_matrix: str | None = None, channel: dict | None = None) -> int:
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
    if channel:
        cmd += ["--channel-url", yt_channels.url_of(channel["handle"]), "--channel-handle", channel["handle"],
                "--channel-type", channel["type"]]
    print(f"\n  Running: {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def _collect_platform(platform: str, runs: int, episodes_per_run: int, auto: bool = False,
                      yt_matrix: str | None = None) -> None:
    not_live: set[str] = set()   # yt channels whose session failed to start this invocation (tried once each)
    for i in range(runs):
        print(f"\n[{platform.upper()} — run {i+1}/{runs}]")
        target_state = most_needed_state(platform, _RUNS_DIR, yt_matrix=yt_matrix)
        channel = yt_channels.next_channel(_RUNS_DIR, exclude=not_live) if platform == "youtube" else None
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
            balance_report(platform, _RUNS_DIR, yt_matrix=yt_matrix)
            return
        if rc != 0:
            print("  Collection returned non-zero — check logs above.")
            if channel:
                not_live.add(channel["handle"])
        balance_report(platform, _RUNS_DIR, yt_matrix=yt_matrix)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Orchestrate QoE data collection with automatic balance targeting"
    )
    group = parser.add_mutually_exclusive_group(required=True)
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

    platforms = list(PLATFORM_TRANSITIONS) if args.all_platforms else [args.platform]

    if args.report_only:
        for p in platforms:
            balance_report(p, _RUNS_DIR, yt_matrix=args.yt_matrix)
        sys.exit(0)

    for p in platforms:
        eps = args.episodes if args.episodes is not None else EPISODES_V2.get(p, 5)
        _collect_platform(p, runs=args.runs, episodes_per_run=eps, auto=args.auto, yt_matrix=args.yt_matrix)
