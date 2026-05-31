#!/usr/bin/env python3
"""
Orchestrate multi-session QoE data collection with adaptive state targeting.

After each completed session it analyzes the state distribution and picks the
most under-represented state as the forced starting point for the next session,
gradually driving the dataset toward a uniform distribution across all 10 states.

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

_RUNS_DIR = Path(__file__).parent.parent / "data"


def _prompt_ready(platform: str, initial_state: str | None) -> bool:
    state_hint = f"starting at state {initial_state}" if initial_state else "weighted-random start"
    print(f"\n  Platform : {platform.upper()}")
    print(f"  Strategy : {state_hint}")
    print(f"\n  Open {platform.capitalize()} Live in Chrome and navigate to a live stream.")
    ans = input("  Press Enter when ready  (or 'q' to abort this run): ").strip().lower()
    return ans != "q"


def _run_collection(platform: str, initial_state: str | None, episodes: int) -> bool:
    cmd = [
        sys.executable, "exp.py",
        "--platform", platform,
        "--episodes", str(episodes),
    ]
    if initial_state:
        cmd += ["--initial-state", initial_state]
    print(f"\n  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode == 0


def _collect_platform(platform: str, runs: int, episodes_per_run: int) -> None:
    for i in range(runs):
        print(f"\n[{platform.upper()} — run {i+1}/{runs}]")
        target_state = most_needed_state(platform, _RUNS_DIR)
        if not _prompt_ready(platform, target_state):
            print("  Skipped.")
            continue
        ok = _run_collection(platform, initial_state=target_state, episodes=episodes_per_run)
        if not ok:
            print("  Collection returned non-zero — check logs above.")
        balance_report(platform, _RUNS_DIR)


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
        default=5,
        help=(
            "State transitions (episodes) per session. "
            "Each episode is ~60 s, so 5 episodes ≈ 5 minutes (default: 5)."
        ),
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
            balance_report(p, _RUNS_DIR)
        sys.exit(0)

    for p in platforms:
        _collect_platform(p, runs=args.runs, episodes_per_run=args.episodes)
