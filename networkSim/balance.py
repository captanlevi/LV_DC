#!/usr/bin/env python3
"""
Analyze Markov-chain stationary distributions and actual collected-data coverage.

Run from the networkSim/ directory:
    python balance.py                          # all platforms
    python balance.py --platform youtube       # one platform
    python balance.py --data-dir /path/to/runs # custom data location
"""
import json
import random
import argparse
from pathlib import Path

from utils.shaping import (
    YOUTUBE_TRANSITIONS,
    TWITCH_TRANSITIONS,
    TIKTOK_TRANSITIONS,
    BILIBILI_TRANSITIONS,
)

PLATFORM_TRANSITIONS: dict[str, dict[str, dict[str, float]]] = {
    "youtube": YOUTUBE_TRANSITIONS,
    "tiktok": TIKTOK_TRANSITIONS,
    "twitch": TWITCH_TRANSITIONS,
    "bilibili": BILIBILI_TRANSITIONS,
}

_DEFAULT_DATA_DIR = Path(__file__).parent.parent / "data"


# ---------------------------------------------------------------------------
# Stationary distribution
# ---------------------------------------------------------------------------

def stationary_distribution(transitions: dict[str, dict[str, float]]) -> dict[str, float]:
    """Power iteration: find π s.t. πP = π."""
    states = sorted(transitions.keys(), key=int)
    pi: dict[str, float] = {s: 1.0 / len(states) for s in states}
    for _ in range(6000):
        new_pi: dict[str, float] = {s: 0.0 for s in states}
        for s in states:
            for t, prob in transitions[s].items():
                new_pi[t] += pi[s] * prob
        pi = new_pi
    return pi


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_run(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def time_weighted_dist(labels: list[dict]) -> dict[str, float]:
    """Fraction of wall-clock time spent in each state for one run."""
    if len(labels) < 2:
        return {labels[0]["state"]: 1.0} if labels else {}
    state_time: dict[str, float] = {}
    for i in range(len(labels) - 1):
        s = str(labels[i]["state"])
        dur = labels[i + 1]["timestamp"] - labels[i]["timestamp"]
        state_time[s] = state_time.get(s, 0.0) + dur
    total = sum(state_time.values())
    return {s: t / total for s, t in state_time.items()}


def aggregate_dists(dists: list[dict[str, float]]) -> dict[str, float]:
    if not dists:
        return {}
    keys: set[str] = set()
    for d in dists:
        keys.update(d.keys())
    return {k: sum(d.get(k, 0.0) for d in dists) / len(dists) for k in keys}


def load_platform_data(platform: str, data_dir: Path) -> tuple[dict[str, float], int]:
    platform_dir = data_dir / platform
    runs = sorted(platform_dir.glob("*/net_labels.txt")) if platform_dir.exists() else []
    dists = []
    for f in runs:
        if f.stat().st_size > 0:
            labels = load_run(f)
            if labels:
                dists.append(time_weighted_dist(labels))
    return aggregate_dists(dists), len(runs)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def balance_report(platform: str, data_dir: Path = _DEFAULT_DATA_DIR) -> None:
    transitions = PLATFORM_TRANSITIONS[platform]
    states = sorted(transitions.keys(), key=int)
    n = len(states)
    target = 1.0 / n

    stationary = stationary_distribution(transitions)
    actual, run_count = load_platform_data(platform, data_dir)

    print(f"\n{'='*64}")
    print(f"  {platform.upper()}  ({run_count} collected runs)")
    print(f"{'='*64}")
    print(f"  {'St':>3}  {'Stationary':>10}  {'Actual':>8}  {'Target':>8}  Status")
    print(f"  {'-'*3}  {'-'*10}  {'-'*8}  {'-'*8}  {'-'*8}")
    for s in states:
        stat_p = stationary.get(s, 0.0)
        act_p = actual.get(s, 0.0) if actual else 0.0
        if not actual:
            status = "no data"
        elif act_p < target * 0.5:
            status = "UNDER"
        elif act_p > target * 1.5:
            status = "OVER"
        else:
            status = "ok"
        print(f"  {s:>3}  {stat_p:>9.1%}  {act_p:>8.1%}  {target:>8.1%}  {status}")

    print()

    # Chain-level warnings
    chain_under = [s for s in states if stationary.get(s, 0.0) < target * 0.5]
    if chain_under:
        print(f"  Markov chain under-represents states {chain_under}")
        print(f"  → Force collection runs starting at these states to compensate.")
    else:
        print(f"  Markov chain stationary distribution: balanced")

    # Data-level warnings
    if actual:
        data_under = [s for s in states if actual.get(s, 0.0) < target * 0.5]
        if data_under:
            print(f"  Collected data under-represents states {data_under}")
            print(f"  → Run more sessions targeting these states.")
    print()


def most_needed_state(platform: str, data_dir: Path = _DEFAULT_DATA_DIR) -> str | None:
    """Return the state with the largest gap below the uniform target, or None if balanced."""
    transitions = PLATFORM_TRANSITIONS[platform]
    states = sorted(transitions.keys(), key=int)
    target = 1.0 / len(states)

    actual, run_count = load_platform_data(platform, data_dir)
    if not actual:
        return random.choice(states[len(states) // 2:])

    deficit = {s: target - actual.get(s, 0.0) for s in states}
    worst = max(deficit, key=deficit.get)
    return worst if deficit[worst] > target * 0.2 else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze QoE collection balance")
    parser.add_argument(
        "--platform",
        choices=list(PLATFORM_TRANSITIONS) + ["all"],
        default="all",
    )
    parser.add_argument(
        "--data-dir",
        default=str(_DEFAULT_DATA_DIR),
        help="Root data directory containing per-platform subdirs (default: data/)",
    )
    args = parser.parse_args()

    platforms = list(PLATFORM_TRANSITIONS) if args.platform == "all" else [args.platform]
    for p in platforms:
        balance_report(p, Path(args.data_dir))
