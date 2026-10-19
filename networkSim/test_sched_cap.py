#!/usr/bin/env python3
"""--batch scheduler (supervisor 2026-10-17): furthest-behind first, at most MAX_CONSECUTIVE_SAME_PLATFORM sessions in a
row on one platform, and a staleness floor (a platform absent from the last STALE_AFTER_SESSIONS sessions goes next).
No browser or capture needed.

    python3 test_sched_cap.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orchestrate as O  # noqa: E402


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    return bool(cond)


def simulate(accepted, targets, n, pass_on=lambda p: False, recent=None):
    """Run the picker n times; a session on p adds a PASS iff pass_on(p)."""
    acc, recent, seq = dict(accepted), list(recent or []), []
    for _ in range(n):
        todo = [p for p in O.BATCH_ORDER if p in targets and acc[p] < targets[p]]
        p = O.next_platform(todo, lambda q: acc[q] / targets[q], recent)
        recent = (recent + [p])[-10:]
        seq.append(p)
        acc[p] += pass_on(p)
    return seq


def main():
    ok = True
    t = {"youtube": 100, "twitch": 80, "tiktok": 80}
    # chaos 16:40: tt far behind and never passing -> without the cap every session is tt
    seq = simulate({"youtube": 23, "twitch": 6, "tiktok": 4}, t, 12)
    ok &= check(seq[:3] == ["tiktok", "tiktok", "twitch"], f"cap breaks a tt run after 2: {seq[:3]}")
    ok &= check(all(seq[i:i + 3].count(seq[i]) < 3 for i in range(len(seq) - 2)), f"no 3 in a row: {seq}")
    # chaos 20:40: tt and twitch both far below yt -> without the floor yt never ran (7.5 h); now at least every 5th
    ok &= check(all("youtube" in seq[i:i + 5] for i in range(len(seq) - 4)), f"yt in every 5 sessions: {seq}")
    # the floor survives a restart: recent read from disk says yt has not run in the last 4 -> yt next
    seq = simulate({"youtube": 23, "twitch": 8, "tiktok": 7}, t, 1,
                   recent=["tiktok", "twitch", "tiktok", "twitch"])
    ok &= check(seq == ["youtube"], f"stale yt goes first after a restart: {seq}")
    # with only one platform left, it may run back to back
    seq = simulate({"youtube": 100, "twitch": 80, "tiktok": 4}, t, 4)
    ok &= check(seq == ["tiktok"] * 4, f"a lone platform is not blocked: {seq}")
    # ordering unchanged when neither rule applies
    r = {"youtube": 0.5, "twitch": 0.1}.get
    ok &= check(O.next_platform(["youtube", "twitch"], r, []) == "twitch", "furthest behind first")
    ok &= check(O.next_platform(["youtube", "twitch"], {"youtube": 0.1, "twitch": 0.1}.get, []) == "youtube",
                "BATCH_ORDER breaks ties")
    ok &= check(O.next_platform(["youtube", "twitch"], r, ["twitch"]) == "twitch", "below the cap the streak continues")
    # recent_platforms reads PASS and _failed sessions in started_at order
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel, t0 in [("youtube/yt_1", 1.0), ("_failed/tiktok/ti_1", 2.0), ("twitch/tw_1", 3.0)]:
            (root / rel).mkdir(parents=True)
            (root / rel / "meta.json").write_text(json.dumps({"started_at": t0}))
        got = O.recent_platforms(root)
        ok &= check(got == ["youtube", "tiktok", "twitch"], f"recent_platforms from disk: {got}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def test_main():
    """The script's own checks under pytest too (levi t58 T-1: the apply gate runs both runners)."""
    assert main() in (0, None)


if __name__ == "__main__":
    sys.exit(main())
