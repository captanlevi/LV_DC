#!/usr/bin/env python3
"""t53 trio layout "composite" (pilot-1, probe v3): the calibration finds the session's window by the browser pid and
places the viewport in its frame by matching the window's own CDP capture (accepted as the X11 calibration is; a frame
holding another window's pixels is refused); each shot crops the player rect from the frame's pixmap, with a staleness
record (the crop unchanged while the playhead advanced). X is stubbed (xcomposite.windows_of_pid / grab); the ctypes
structures are checked for LP64 Linux.

    python3 test_composite_shots.py        (exit 0 = pass)
"""
import base64
import ctypes
import io
import json
import sys
import types
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.SimpleNamespace(WebSocket=object, WebSocketTimeoutException=Exception)
import browser as B  # noqa: E402
import session_meta  # noqa: E402
import xcomposite as X  # noqa: E402
from PIL import Image  # noqa: E402


def _page_pixels(seed, h=400, w=600):
    rng = np.random.default_rng(seed)
    tiles = rng.integers(0, 2, (h // 20, w // 20)) * 200 + 25
    return np.kron(tiles, np.ones((20, 20))).astype(np.uint8)


class _Page:
    def __init__(self, cdp, ct=10.0):
        buf = io.BytesIO()
        Image.fromarray(cdp).save(buf, format="PNG")
        self.png, self.ct = base64.b64encode(buf.getvalue()).decode(), ct

    def eval(self, js, await_promise=False):
        if js is B._PLAYER_RECT_JS:
            return json.dumps({"player": {"rect": [100, 50, 320, 180], "inner": [600, 400], "dpr": 1},
                               "viewport": [600, 400], "dpr": 1, "fullscreen": False, "screen": [1920, 1080]})
        if js is B._PLAYHEAD_JS:
            return self.ct
        return None

    def call(self, method, params=None):
        return {"data": self.png} if method == "Page.captureScreenshot" else {}


def _frame(page_px, at=(8, 90), size=(640, 520)):
    f = np.full((size[1], size[0]), 60, np.uint8)                     # window decorations / tab strip
    f[at[1]:at[1] + page_px.shape[0], at[0]:at[0] + page_px.shape[1]] = page_px
    return Image.fromarray(f).convert("RGB")


def _stub(frame_img, wins=None, pid=4242):
    saved = (session_meta.chrome_cmdline, X.windows_of_pid, X.grab)
    session_meta.chrome_cmdline = lambda cdp_base=None: {"pid": pid, "argv": []}
    X.windows_of_pid = lambda p: (wins if wins is not None else
                                  [{"client": "0x600005", "frame": "0x600003", "size": [624, 430], "frame_size": [640, 520]}])
    X.grab = lambda f: (frame_img, {"frame": f, "size": list(frame_img.size), "depth": 32, "bpp": 32})
    return saved


def _restore(saved):
    session_meta.chrome_cmdline, X.windows_of_pid, X.grab = saved


def test_calibration():
    own, other = _page_pixels(1), _page_pixels(2)
    saved = _stub(_frame(own))
    try:
        r = B.calibrate_composite(_Page(own))
        assert r["accepted"] and r["viewport_in_frame"] == [8, 90] and r["viewport_px"] == [600, 400], r
        assert r["frame"] == "0x600003" and r["browser_pid"] == 4242 and r["residual"] == 0.0, r
        _restore(saved)
        saved = _stub(_frame(other))                                   # the frame holds another window's pixels
        r = B.calibrate_composite(_Page(own))
        assert not r["accepted"], r
        _restore(saved)
        saved = _stub(_frame(own), wins=[])
        r = B.calibrate_composite(_Page(own))
        assert not r["accepted"] and r["why"].startswith("no viewable window"), r
    finally:
        _restore(saved)


def test_shot_crop_and_staleness():
    own = _page_pixels(1)
    origin = {"frame": "0x600003", "viewport_in_frame": [8, 90], "viewport_px": [600, 400], "accepted": True}
    player = {"rect": [100, 50, 320, 180], "inner": [600, 400], "dpr": 1}
    saved = _stub(_frame(own))
    try:
        prev: dict = {}
        page = _Page(own, ct=10.0)
        data, px, region, crop, rec = B._composite_shot(origin, player, page, prev)
        assert data and px == [320, 180] and region == (108, 140, 320, 180) and crop == "player", (px, region, crop)
        got = np.asarray(Image.open(io.BytesIO(data)).convert("L"), float)
        want = own[50:230, 100:420].astype(float)
        assert got.shape == want.shape and float(np.abs(got - want).mean()) < 6.0          # JPEG of the player crop
        assert rec == {"frame": "0x600003", "diff_prev": None, "ct_step_s": None, "stale": False}, rec
        page.ct = 15.0                                                 # the playhead moved; the pixmap did not
        rec = B._composite_shot(origin, player, page, prev)[4]
        assert rec["stale"] is True and rec["diff_prev"] == 0.0 and rec["ct_step_s"] == 5.0, rec
        X.grab = lambda f: (_frame(_page_pixels(3)), {})               # new pixels: live
        page.ct = 20.0
        rec = B._composite_shot(origin, player, page, prev)[4]
        assert rec["stale"] is False and rec["diff_prev"] > 10, rec
        page.ct = 20.0                                                 # paused: unchanged pixels are not stale
        rec = B._composite_shot(origin, player, page, prev)[4]
        assert rec["stale"] is False and rec["ct_step_s"] == 0.0, rec
        X.grab = lambda f: (Image.new("RGB", (640, 520), (0, 0, 0)), {})   # a blank pixmap: no shot (CDP fallback)
        data, _, _, _, rec = B._composite_shot(origin, player, page, prev)
        assert data is None and rec["blank"] is True
    finally:
        _restore(saved)


def test_x_structs():
    if ctypes.sizeof(ctypes.c_long) != 8:
        return                                                         # chaos: LP64 Linux
    assert ctypes.sizeof(X._XWindowAttributes) == 136 and X._XErrorEvent.error_code.offset == 32
    assert X._XImage.bits_per_pixel.offset == 48 and X._XImage.data.offset == 16
    assert ctypes.sizeof(X._XClientMessageEvent) == 96 and X._XClientMessageEvent.l.offset == 56 and ctypes.sizeof(X._XEvent) == 192


def test_fullscreen_keeper():
    """t53 (a): the keeper finds another window on top at the screen's centre, activates its own and records it; on top,
    it does nothing; the window comes from the browser pid."""
    import time as _t
    seq = ["0x2000003", "0x5000001", "0x5000001", "0x2000003"]       # ours, a sibling's new window twice, ours again
    sent = []
    saved = (X.window_at, X.activate, B.FS_KEEPER_S)
    try:
        X.window_at = lambda x, y: seq.pop(0) if seq else "0x2000003"
        X.activate = lambda c: sent.append(c) or True
        B.FS_KEEPER_S = 0.01
        k = B.FullscreenKeeper("0x2000003", "0x2000003", (960, 540)).start()
        for _ in range(200):
            if k.checks >= 6:
                break
            _t.sleep(0.01)
        k.stop()
        r = k.record()
        assert r["not_on_top"] == 2 and sent == ["0x2000003", "0x2000003"], (r, sent)
        assert [e["top_was"] for e in r["events"]] == ["0x5000001", "0x5000001"] and r["centre"] == [960, 540], r
        assert k.on_top() == (True, "0x2000003")
    finally:
        X.window_at, X.activate, B.FS_KEEPER_S = saved
    saved = _stub(_frame(_page_pixels(1)))
    real_wa = X.window_at
    try:
        X.window_at = lambda x, y: "0x600003"
        rec = B._start_fullscreen_keeper(_Page(_page_pixels(1)))
        assert rec["started"] and rec["frame"] == "0x600003" and rec["centre"] == [960, 540] and rec["pid"] == 4242, rec
        B.FS_KEEP.pop("keeper").stop()
    finally:
        X.window_at = real_wa
        _restore(saved)


def main() -> int:
    test_calibration()
    test_shot_crop_and_staleness()
    test_fullscreen_keeper()
    test_x_structs()
    print("test_composite_shots: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
