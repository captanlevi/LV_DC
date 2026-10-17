#!/usr/bin/env python3
"""Screenshot capture (browser.py, supervisor 2026-10-17): the page viewport's position on the X screen from window
metrics (chaos: Chrome 1850x1053 at +54+17 with the tab strip, toolbar and the --no-sandbox infobar above the page), and
the X11 grab falling back (None) where it cannot run.

    python3 test_shots.py        (exit 0 = pass)
"""
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402


def main() -> int:
    v = {"sx": 54, "sy": 17, "ow": 1850, "oh": 1053, "iw": 1850, "ih": 868, "dpr": 1}
    assert B._viewport_on_screen(v) == (54, 17 + 1053 - 868, 1850, 868)
    v2 = {"sx": 0, "sy": 0, "ow": 1290, "oh": 900, "iw": 1280, "ih": 780, "dpr": 2}   # a 5 px frame, HiDPI
    assert B._viewport_on_screen(v2) == (10, 2 * 115, 2560, 1560)
    assert B._viewport_on_screen({}) is None
    # the player rectangle on screen: viewport origin + the <video> rect, clipped to the viewport
    vp = (54, 202, 1850, 868)
    assert B._player_on_screen(vp, {"rect": [10, 20, 1238, 696], "inner": [1850, 868]}) == (64, 222, 1238, 696)
    assert B._player_on_screen(vp, {"rect": [1800, 0, 400, 300], "inner": [1850, 868]}) == (1854, 202, 50, 300)
    assert B._player_on_screen(vp, None) is None
    if sys.platform == "darwin":
        assert B._grab_x11((0, 0, 100, 100)) is None   # no X display here: the CDP fallback takes the shot
    print("test_shots: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
