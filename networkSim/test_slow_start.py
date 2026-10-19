#!/usr/bin/env python3
"""slow_start_shaped (supervisor 2026-10-18, fixture: chaos youtube/yt_35's meta.json entries): a stream entry that did
not sustain play while the shaper sat at state <= 2 inside the startup window keeps class normal and is flagged; the same
entry with bandwidth (state >= 3, or shaping only after the window) stays startup_failure; startup_rates still counts it
as a failed entry (levi). No browser needed.

    python3 test_slow_start.py        (exit 0 = pass)
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entries as E  # noqa: E402

YT_35 = [{"kind": "stream", "t": 1792260252.4365, "url": "https://www.youtube.com/@MonsterJam/live",
          "shaper_at_entry": {"state": "unshaped", "rate_kbit": None},
          "first_shaped_after": {"state": "1", "rate_kbit": 172, "t": 1792260260.7209384},
          "sustained_60s": False, "sustained_at": None}]


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    return bool(cond)


def cls(ents, platform="youtube", abort=None):
    return E.session_class(platform, "PASS", abort, ents, None, None, None, None)


def main():
    ok = True
    ok &= check(E.slow_start_shaped(YT_35), "yt_35: state 1 inside the window -> slow_start_shaped")
    ok &= check(cls(YT_35) == "normal", "yt_35 class -> normal")
    s3 = copy.deepcopy(YT_35); s3[0]["first_shaped_after"]["state"] = "3"
    ok &= check(not E.slow_start_shaped(s3) and cls(s3) == "startup_failure", "state 3 (bandwidth there) -> startup_failure")
    late = copy.deepcopy(YT_35); late[0]["first_shaped_after"]["t"] = YT_35[0]["t"] + 75
    ok &= check(cls(late) == "startup_failure", "shaping only after the 60 s window -> startup_failure")
    none = copy.deepcopy(YT_35); none[0]["first_shaped_after"] = None
    ok &= check(cls(none) == "startup_failure", "no shaped state recorded -> startup_failure")
    good = copy.deepcopy(YT_35); good[0]["sustained_60s"] = True
    ok &= check(not E.slow_start_shaped(good) and cls(good) == "normal", "sustained -> normal, not flagged")
    ok &= check(cls(YT_35, "youtube", "Failed to navigate to a live youtube stream") == "not_live",
                "a not-live abort still wins (precedence unchanged)")
    r = E.startup_rates(YT_35)
    ok &= check(r["stream:unshaped"]["failed"] == 1, f"still a failed entry in startup_rates: {r}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def test_main():
    """The script's own checks under pytest too (levi t58 T-1: the apply gate runs both runners)."""
    assert main() in (0, None)


if __name__ == "__main__":
    sys.exit(main())
