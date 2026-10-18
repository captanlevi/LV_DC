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


def _synthetic(seed=0, flat=False):
    """A 1000x700 screen with a 600x400 viewport at its true origin (210, 167); the window metrics put the model 21 px
    lower and 3 px left (yt_43 / tw_35's shape); the <video> rect's pixels differ between the two captures."""
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(seed)

    def smooth(h, w, cell=12):                       # page-like content: structure at ~cell px, not per-pixel noise
        g = rng.integers(0, 255, (h // cell + 1, w // cell + 1)).astype(np.uint8)
        return np.array(Image.fromarray(g).resize((w, h), Image.BILINEAR))
    screen = smooth(700, 1000, 40)                                               # wallpaper / other windows
    page = np.full((400, 600), 200, np.uint8) if flat else smooth(400, 600)
    page[40:340, 60:520] = smooth(300, 460)                                      # the picture, at X11 time
    screen[167:567, 210:810] = page
    cdp = page.copy()
    cdp[40:340, 60:520] = smooth(300, 460)                                       # the picture moved by CDP time
    return screen, cdp


class _CalPage:
    def __init__(self, cdp, fullscreen=False):
        import base64
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(cdp).save(buf, format="PNG")
        self.png = base64.b64encode(buf.getvalue()).decode()
        self.fs = fullscreen

    def eval(self, js, await_promise=False):
        import json
        if js is B._PLAYER_RECT_JS:
            return json.dumps({"player": {"rect": [60, 40, 460, 300], "inner": [600, 400], "dpr": 1},
                               "viewport": [600, 400], "dpr": 1, "fullscreen": self.fs, "screen": [1000, 700]})
        # model viewport (207, 188): side 0, top = oh - ih = 188 - sy
        return json.dumps({"sx": 207, "sy": 0, "ow": 600, "oh": 588, "iw": 600, "ih": 400, "dpr": 1})

    def call(self, method, params=None):
        return {"data": self.png} if method == "Page.captureScreenshot" else {}


def test_origin_calibration():
    """t37 (supervisor 2026-10-18): one CDP viewport capture matched against the X11 grab around the model's viewport,
    over the page outside the <video> rect, gives the origin correction; a featureless page or a fullscreen player is
    not calibrated by matching; the verdict's thresholds are frozen constants."""
    import PIL.ImageGrab as IG
    from PIL import Image
    assert (B.SHOT_CALIB_MARGIN_PX, B.SHOT_CALIB_SCALE, B.SHOT_CALIB_MAX_RESIDUAL, B.SHOT_CALIB_MIN_GAP) == (64, 4, 8.0, 4.0)
    real = IG.grab
    try:
        for flat, ok in ((False, True), (True, False)):
            screen, cdp = _synthetic(flat=flat)
            IG.grab = lambda bbox=None, xdisplay=None, s=screen: Image.fromarray(s).crop(bbox)
            r = B.calibrate_shot_origin(_CalPage(cdp))
            assert r["accepted"] is ok, r
            if ok:                                   # true (210, 167) - model (207, 188)
                assert r["offset"] == [3, -21] and r["residual"] == 0.0 and r["model_viewport"] == [207, 188, 600, 400], r
                assert 0 < r["compared_share"] < 0.1 and r["gap"] > 20, r          # the page beside the picture
        r = B.calibrate_shot_origin(_CalPage(cdp, fullscreen=True))
        assert r["accepted"] and r["offset"] == [0, 0] and r["method"].startswith("fullscreen"), r
    finally:
        IG.grab = real
    assert B.calibration_verdict(8.0, 4.0) and not B.calibration_verdict(8.1, 9.0) and not B.calibration_verdict(1.0, 3.9)
    assert not B.calibration_verdict(None, None)


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
    # device scale 1.5 (geometry theater:1920x1080@1.5; levi's question on b311277): the index's player rect is CSS px
    # with its dpr; the X11 region is device px. Laptop check 2026-10-18 (Chrome --force-device-scale-factor=1.5, a
    # <video> at CSS [100, 50, 640, 360] in a 1200x713 CSS viewport): the captured frame was 1800x1070 and the box sat
    # at [150, 75, 960, 540], which _player_on_screen returns
    assert B._player_on_screen((0, 0, 1800, 1070), {"rect": [100, 50, 640, 360], "inner": [1200, 713], "dpr": 1.5}) == \
        (150, 75, 960, 540)
    v15 = {"sx": 22, "sy": 47, "ow": 1200, "oh": 800, "iw": 1200, "ih": 713, "dpr": 1.5}
    assert B._viewport_on_screen(v15) == (33, 201, 1800, 1070)                 # window metrics (CSS px) x dpr
    if sys.platform == "darwin":
        assert B._grab_x11((0, 0, 100, 100)) is None   # no X display here: the CDP fallback takes the shot
    test_origin_calibration()
    print("test_shots: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
