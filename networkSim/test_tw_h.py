#!/usr/bin/env python3
"""TW-H, the twitch matrix whose band reaches 1080p (prereg d32a48ce item 4; levi 18:14): state 11's rate_max reaches
>= 1.25 x the highest t58 twitch 1080p variant BANDWIDTH (9147536 bps, shagun's read sha 3c63b074113b), rows 1-8 are
TW-M's, every row sums to 1, state 11 carries stationary mass, coverage_report's twitch Q_1080p band holds it and TW-H
qualifies there, twitch Q_1080p is no longer ABR-only, and five slots at state 11's top stay under the link capacity.

    python3 test_tw_h.py        (exit 0 = pass)
"""
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coverage_report as CR  # noqa: E402
from utils import shaping as SH  # noqa: E402

MAX_1080P_BPS = 9147536            # shagun 18:14, tw_100078 squeex master line 25 (read sha 3c63b074113b)
LINK_KBIT = 104100                 # levi 18:14: the box's link capacity
TRIO_MAX_SLOTS = 5


def test_tw_h():
    need = math.ceil(1.25 * MAX_1080P_BPS / 1000)
    assert need == 11435 and SH.SCENARIOS["11"].rate_max >= need, SH.SCENARIOS["11"]
    assert SH.SCENARIOS["11"].rate_min >= SH.SCENARIOS["10"].rate_max - 1e-9            # above state 10, no overlap
    m, cap, init = SH.TWITCH_MATRICES["TW-H"]
    assert (cap, init) == (2, "stationary") and set(m) == {str(i) for i in range(1, 12)}
    assert all(abs(sum(r.values()) - 1.0) < 1e-9 for r in m.values())
    assert all(m[s] == SH.TWITCH_TRANSITIONS_M[s] for s in map(str, range(1, 9)))      # the base matrix's shape
    pi = SH.stationary_distribution(m)
    assert pi["11"] > 0.10, pi
    assert all("11" not in mm for mats in (SH.YOUTUBE_MATRICES, SH.TIKTOK_MATRICES) for mm, _, _ in mats.values())
    assert all("11" not in mm for k, (mm, _, _) in SH.TWITCH_MATRICES.items() if k != "TW-H")
    band = CR.class_band("twitch", "Q_1080p")
    assert "11" in band["band"], band
    bm = CR.band_map("twitch", SH.TWITCH_MATRICES, sorted(SH.TWITCH_MATRICES))
    assert "TW-H" in bm["Q_1080p"]["qualifies"] and bm["Q_1080p"]["band"] == ["11"], bm["Q_1080p"]
    assert ("twitch", "Q_1080p") not in CR.ABR_ONLY
    assert TRIO_MAX_SLOTS * SH.SCENARIOS["11"].rate_max < LINK_KBIT


def main() -> int:
    test_tw_h()
    print("test_tw_h: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
