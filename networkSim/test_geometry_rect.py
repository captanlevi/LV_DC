#!/usr/bin/env python3
"""Player position for the motion crop (supervisor / levi 2026-10-17, brad's R-O audit; Addendum O amendment 2):
_GEOMETRY_JS records the largest visible <video>'s bounding rect and the player's control band (yt / twitch), the shot
loop's _PLAYER_RECT_JS returns the viewport even without a player, and geometry_scales puts the rect in screenshot
pixels. JS under node with a stub DOM.

    python3 test_geometry_rect.py        (exit 0 = pass)
"""
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402

STUB = r"""
const cfg = JSON.parse(process.argv[2]);
const el = (r, extra) => Object.assign({getBoundingClientRect: () => ({x: r[0], y: r[1], width: r[2], height: r[3]}),
                                        offsetParent: {}, clientWidth: r[2], clientHeight: r[3]}, extra || {});
const vids = cfg.videos.map(r => el(r));
const yb = cfg.yt_band ? el(cfg.yt_band) : null;
const tb = cfg.tw_band ? el(cfg.tw_band, {dataset: {aVisible: cfg.tw_visible ? 'true' : 'false'}}) : null;
const mp = cfg.yt_band ? {classList: {contains: c => c === 'ytp-autohide' && cfg.yt_autohide}} : null;
global.window = {innerWidth: 1280, innerHeight: 720, outerWidth: 1280, outerHeight: 800, devicePixelRatio: cfg.dpr};
global.screen = {width: 1920, height: 1080};
global.document = {
  querySelectorAll: s => s === 'video' ? vids : [],
  querySelector: s => ({'#movie_player video': cfg.yt_band ? vids[0] : null, 'video': vids[0] || null,
                        '#movie_player .ytp-chrome-bottom': yb, '[data-a-target="player-controls"]': tb,
                        '#movie_player': mp, 'ytd-watch-flexy': null})[s] ?? null,
};
console.log(eval(process.argv[1]));
"""


def js(src, **cfg):
    cfg = {"videos": [], "dpr": 1, **cfg}
    out = subprocess.run([shutil.which("node"), "-e", STUB, src, json.dumps(cfg)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def main() -> int:
    if not shutil.which("node"):
        print("SKIP: node not found")
        return 0
    g = js(B._GEOMETRY_JS, videos=[[10, 20, 160, 90], [24, 80, 853, 480]], yt_band=[24, 512, 853, 48], yt_autohide=True)
    assert g["player_rect"] == [24, 80, 853, 480], g                       # the largest visible video
    assert g["control_band"] == {"source": "yt .ytp-chrome-bottom", "rect": [24, 512, 853, 48], "height": 48,
                                 "showing": False}, g
    g = js(B._GEOMETRY_JS, videos=[[0, 50, 1274, 717]], tw_band=[0, 707, 1274, 60], tw_visible=True)
    assert g["control_band"]["source"].startswith("twitch") and g["control_band"]["showing"] is True, g
    assert js(B._GEOMETRY_JS, videos=[[100, 40, 783, 604]])["control_band"] is None   # tt: the inset rule
    # the shot loop: player rect and viewport in one eval; the viewport also with no player
    r = js(B._PLAYER_RECT_JS, videos=[[24, 80, 853, 480]], dpr=2)
    assert r == {"player": {"rect": [24, 80, 853, 480], "inner": [1280, 720], "dpr": 2}, "viewport": [1280, 720], "dpr": 2}, r
    assert js(B._PLAYER_RECT_JS) == {"player": None, "viewport": [1280, 720], "dpr": 1}
    # screenshot pixels: viewport 1280 CSS px at dpr 2 = 2560 device px -> 640 px shots (scale 0.25)
    sc = B.geometry_scales({"player_rect": [24, 80, 853, 480], "viewport": [1280, 720], "device_pixel_ratio": 2})
    assert sc["player_rect_device_px"] == [48, 160, 1706, 960] and sc["viewport_shot_scale"] == 0.25, sc
    assert sc["player_in_viewport_shot_px"] == [12, 40, 426.5, 240], sc
    assert sc["x11_player_shot_scale"] == round(640 / 1706, 5), sc
    assert B.geometry_scales({"player_rect": None, "viewport": [1280, 720]}) is None
    print("test_geometry_rect: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
