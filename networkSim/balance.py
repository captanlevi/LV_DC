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
    YOUTUBE_MATRICES,
    PLATFORM_MATRICES,
)

PLATFORM_TRANSITIONS: dict[str, dict[str, dict[str, float]]] = {
    "youtube": YOUTUBE_TRANSITIONS,
    "tiktok": TIKTOK_TRANSITIONS,
    "twitch": TWITCH_TRANSITIONS,
    "bilibili": BILIBILI_TRANSITIONS,
}

from data_root import DATA_ROOT as _DEFAULT_DATA_DIR  # noqa: E402  (one setting: data_root.py)


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
    """The schedule's rows: a J join row (phase "join", levi 15:25) is never coverage."""
    with open(path) as f:
        return [r for r in (json.loads(line) for line in f if line.strip()) if r.get("phase") is None]


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


def _selected(run: Path, select) -> bool:
    """select(meta) on the run's meta.json (None: every run; an unreadable meta.json: not selected)."""
    if select is None:
        return True
    try:
        return bool(select(json.loads((run.parent / "meta.json").read_text())))
    except (OSError, ValueError):
        return False


def load_platform_data(platform: str, data_dir: Path, select=None) -> tuple[dict[str, float], int]:
    """select(meta) -> bool: only the runs it keeps (the trio: its sessions of record, levi 00265 / t53 2026-10-19)."""
    platform_dir = data_dir / platform
    runs = sorted(platform_dir.glob("*/net_labels.txt")) if platform_dir.exists() else []
    runs = [f for f in runs if _selected(f, select)]
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

def balance_report(platform: str, data_dir: Path = _DEFAULT_DATA_DIR, yt_matrix: str | None = None,
                   matrix_id: str | None = None, select=None) -> None:
    matrix_id = matrix_id or (yt_matrix if platform == "youtube" else None)
    m = _matrix(platform, matrix_id)
    transitions = m[0] if m else PLATFORM_TRANSITIONS[platform]
    states = sorted(transitions.keys(), key=int)
    targets = balance_target(platform, matrix_id)

    stationary = stationary_distribution(transitions)
    actual, run_count = load_platform_data(platform, data_dir, select)

    print(f"\n{'='*64}")
    print(f"  {platform.upper()}  ({run_count} collected runs)")
    print(f"{'='*64}")
    print(f"  {'St':>3}  {'Stationary':>10}  {'Actual':>8}  {'Target':>8}  Status")
    print(f"  {'-'*3}  {'-'*10}  {'-'*8}  {'-'*8}  {'-'*8}")
    for s in states:
        target = targets[s]
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
    chain_under = [s for s in states if stationary.get(s, 0.0) < targets[s] * 0.5]
    if chain_under:
        print(f"  Markov chain under-represents states {chain_under}")
        print(f"  → Force collection runs starting at these states to compensate.")
    else:
        print(f"  Markov chain stationary distribution: balanced")

    # Data-level warnings
    if actual:
        data_under = [s for s in states if actual.get(s, 0.0) < targets[s] * 0.5]
        if data_under:
            print(f"  Collected data under-represents states {data_under}")
            print(f"  → Run more sessions targeting these states.")
    print()


def _matrix(platform: str, matrix_id: str | None):
    """(transitions, cap, init rule) of the named matrix for this platform, or None (the platform default)."""
    table = PLATFORM_MATRICES.get(platform, {})
    return table.get(matrix_id) if matrix_id else None


def balance_target(platform: str, matrix_id: str | None = None) -> dict[str, float]:
    """Per-state target share. A matrix with a stationary start (yt A / B, t38; tt TT-M / twitch TW-M, addendum M):
    its stationary distribution. Everything else: uniform (the pre-t38 rule)."""
    m = _matrix(platform, matrix_id)
    if m and m[2] == "stationary":
        return stationary_distribution(m[0])
    states = sorted(PLATFORM_TRANSITIONS[platform].keys(), key=int)
    return {s: 1.0 / len(states) for s in states}


def most_needed_state(platform: str, data_dir: Path = _DEFAULT_DATA_DIR, yt_matrix: str | None = None,
                      matrix_id: str | None = None, select=None, seed: str | None = None,
                      held: dict | None = None, record: dict | None = None) -> str | None:
    """Return the state with the largest gap below its target share (balance_target), or None if balanced.
    A stationary-start matrix returns None (no forced start): exp.py draws the start from the stationary
    distribution, which is the target itself. yt_matrix is the older name of matrix_id (yt only).
    seed (t83 (5)): the empty-history draw comes from random.Random(f"balance:{seed}") so it replays; None keeps the
    unseeded draw (single mode). held (t83 (7)): {state: n}, the start states the running sibling slots already hold
    (their net_labels.txt is written only at session end, so the history cannot see them); a state a sibling holds is
    passed over for the next qualifying one, and kept when none is left. record: filled with the pick's inputs."""
    matrix_id = matrix_id or (yt_matrix if platform == "youtube" else None)
    m = _matrix(platform, matrix_id)
    if m and m[2] == "stationary":
        return None
    transitions = PLATFORM_TRANSITIONS[platform]
    states = sorted(transitions.keys(), key=int)
    target = balance_target(platform, matrix_id)
    held = {s: n for s, n in (held or {}).items() if n}
    rec = record if record is not None else {}

    actual, run_count = load_platform_data(platform, data_dir, select)
    rec.update(rule="balance", history_runs=run_count, seed=seed, held=held)
    if not actual:
        upper = states[len(states) // 2:]
        free = [s for s in upper if s not in held]
        pick = (random.Random(f"balance:{seed}") if seed is not None else random).choice(free or upper)
        rec.update(branch="empty_history", choices=free or upper, state=pick)
        return pick

    deficit = {s: target[s] - actual.get(s, 0.0) for s in states}
    worst = max(deficit, key=deficit.get)
    if deficit[worst] <= target[worst] * 0.2:
        rec.update(branch="balanced", state=None)
        return None
    ranked = [s for s in sorted(states, key=lambda s: -deficit[s]) if deficit[s] > target[s] * 0.2]
    pick = next((s for s in ranked if s not in held), worst)
    rec.update(branch="deficit", qualifying=ranked, passed_over=ranked[:ranked.index(pick)],
               state=pick)
    return pick


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
    parser.add_argument("--yt-matrix", choices=["A", "B", "current"], default="A",
                        help="yt: report against this matrix's stationary distribution (t38; default A)")
    args = parser.parse_args()

    platforms = list(PLATFORM_TRANSITIONS) if args.platform == "all" else [args.platform]
    for p in platforms:
        balance_report(p, Path(args.data_dir), yt_matrix=args.yt_matrix)
