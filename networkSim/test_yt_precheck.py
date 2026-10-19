#!/usr/bin/env python3
"""yt pre-session liveness probe (shagun 2026-10-17): page classification on stub pages shaped like peter's laptop
probes (live: isLiveNow true; off air: isLiveNow false, or the /channel/ canonical redirect without isLiveNow; consent
or unknown page: fail open), and the picker re-drawing past positively-not-live channels. No network needed.

    python3 test_yt_precheck.py        (exit 0 = pass)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yt_channels as Y  # noqa: E402

LIVE = '<link rel="canonical" href="https://www.youtube.com/watch?v=abc"> ... "isLiveNow":true, "isLiveContent":true'
OFF_FLAG = '<link rel="canonical" href="https://www.youtube.com/watch?v=old"> ... "isLiveNow":false,"isLiveContent":true'
OFF_REDIRECT = '<link rel="canonical" href="https://www.youtube.com/channel/UCabc123"> no live markers here'
ODD = "<html><body>something else</body></html>"


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    return bool(cond)


def main():
    ok = True
    ok &= check(Y.classify_live_page("https://www.youtube.com/@x/live", LIVE) == "live", "isLiveNow true -> live")
    ok &= check(Y.classify_live_page("https://www.youtube.com/@x/live", OFF_FLAG) == "not_live",
                "isLiveNow false -> not_live (MonsterJam shape)")
    ok &= check(Y.classify_live_page("https://www.youtube.com/@x/live", OFF_REDIRECT) == "not_live",
                "channel canonical, no isLiveNow -> not_live (Sypherpk shape)")
    ok &= check(Y.classify_live_page("https://consent.youtube.com/m?continue=x", LIVE) == "unknown",
                "consent redirect -> unknown (fail open)")
    ok &= check(Y.classify_live_page("https://www.youtube.com/@x/live", ODD) == "unknown", "no markers -> unknown")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        prim = [c for c in Y.YT_CHANNELS if c["role"] == "primary"]
        first = Y.next_channel(d)
        # the first pick is off air -> the picker moves on, logs one skip
        states = {first["handle"]: "not_live"}
        c, skips = Y.pick_live_channel(d, probe=lambda h: states.get(h, "live"))
        ok &= check(c["handle"] != first["handle"] and [s["handle"] for s in skips] == [first["handle"]],
                    f"off-air first pick skipped: {first['handle']} -> {c['handle']}")
        # unknown never skips (fail open)
        c, skips = Y.pick_live_channel(d, probe=lambda h: "unknown")
        ok &= check(c["handle"] == first["handle"] and not skips, "unknown probe runs the first pick")
        # everything off air -> fall back to the first pick (the session fails and strikes as before)
        c, skips = Y.pick_live_channel(d, probe=lambda h: "not_live")
        ok &= check(c["handle"] == first["handle"] and len(skips) >= len(prim), "all off air -> first pick")
        # strike-skipped channels stay excluded
        c, _ = Y.pick_live_channel(d, exclude={first["handle"]}, probe=lambda h: "live")
        ok &= check(c["handle"] != first["handle"], "exclude (strikes) respected")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def test_main():
    """The script's own checks under pytest too (levi t58 T-1: the apply gate runs both runners)."""
    assert main() in (0, None)


if __name__ == "__main__":
    sys.exit(main())
