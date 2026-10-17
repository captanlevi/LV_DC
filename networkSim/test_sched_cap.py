#!/usr/bin/env python3
"""--batch scheduler cap (supervisor 2026-10-17: tt at 4/80 took every session for an hour): furthest-behind first,
but at most MAX_CONSECUTIVE_SAME_PLATFORM sessions in a row on one platform. No browser or capture needed.

    python3 test_sched_cap.py        (exit 0 = pass)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orchestrate as O  # noqa: E402


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    return bool(cond)


def simulate(accepted, targets, n, pass_on=lambda p: False):
    """Run the picker n times; a session on p adds a PASS iff pass_on(p)."""
    acc, last, streak, seq = dict(accepted), None, 0, []
    for _ in range(n):
        todo = [p for p in O.BATCH_ORDER if p in targets and acc[p] < targets[p]]
        p = O.next_platform(todo, lambda q: acc[q] / targets[q], last, streak)
        last, streak = p, (streak + 1 if p == last else 1)
        seq.append(p)
        acc[p] += pass_on(p)
    return seq


def main():
    ok = True
    t = {"youtube": 100, "twitch": 80, "tiktok": 80}
    # chaos 16:40: tt far behind and never passing (room_ended) -> without the cap every session is tt
    seq = simulate({"youtube": 23, "twitch": 6, "tiktok": 4}, t, 9)
    ok &= check(seq[:3] == ["tiktok", "tiktok", "twitch"], f"cap breaks a tt run after 2: {seq[:3]}")
    ok &= check(all(seq[i:i + 3].count(seq[i]) < 3 for i in range(len(seq) - 2)), f"no 3 in a row: {seq}")
    # with only one platform left, it may run back to back
    seq = simulate({"youtube": 100, "twitch": 80, "tiktok": 4}, t, 4)
    ok &= check(seq == ["tiktok"] * 4, f"a lone platform is not blocked: {seq}")
    # the ordering itself is unchanged when no streak is reached
    ok &= check(O.next_platform(["youtube", "twitch"], {"youtube": 0.5, "twitch": 0.1}.get, None, 0) == "twitch",
                "furthest behind first")
    ok &= check(O.next_platform(["youtube", "twitch"], {"youtube": 0.1, "twitch": 0.1}.get, None, 0) == "youtube",
                "BATCH_ORDER breaks ties")
    ok &= check(O.next_platform(["youtube", "twitch"], {"youtube": 0.5, "twitch": 0.1}.get, "twitch", 1) == "twitch",
                "below the cap the streak continues")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
