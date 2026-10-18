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


CAP_STUB = r"""
const [present, pressed, sticky] = JSON.parse(process.argv[2]);
let clicks = 0, state = pressed;
const b = {offsetParent: {}, getAttribute: a => a === 'aria-pressed' ? String(state) : null,
           click() { clicks++; if (!sticky) state = !state; }};
global.document = {querySelector: s => (present && s === '#movie_player .ytp-subtitles-button') ? b : null};
console.log(JSON.stringify({r: eval(process.argv[1]), clicks}));
"""


def captions(present, pressed, sticky=False):
    out = subprocess.run([shutil.which("node"), "-e", CAP_STUB, B._CAPTIONS_OFF_JS, json.dumps([present, pressed, sticky])],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_captions_off():
    """brad 2026-10-17: yt live auto-captions over the player's lower band; the collector turns them off."""
    assert captions(True, True) == {"r": "turned-off", "clicks": 1}
    assert captions(True, False) == {"r": "already-off", "clicks": 0}      # never toggles them on
    assert captions(False, False) == {"r": "no-button", "clicks": 0}
    assert captions(True, True, sticky=True) == {"r": "click-failed", "clicks": 1}


THEATER_STUB = r"""
const [has_flexy, theater, has_button] = JSON.parse(process.argv[2]);
let clicks = 0;
const f = {attrs: new Set(theater ? ['theater'] : []), hasAttribute(a) { return this.attrs.has(a); }};
const b = {click() { clicks++; f.attrs.add('theater'); }};
global.document = {querySelector: s => ({'ytd-watch-flexy': has_flexy ? f : null, '.ytp-size-button': has_button ? b : null})[s] ?? null};
console.log(JSON.stringify({r: JSON.parse(eval(process.argv[1])), clicks}));
"""


def test_theater():
    """supervisor 2026-10-18 (no 1080p on yt): geometry theater:WxH = window W x H, then yt's theater mode, once."""
    import geometry as G
    assert G.parse("theater:1920x1080") == ("theater", 1920, 1080)
    assert "theater:1920x1080" in G.GEOMETRY_CHOICES["youtube"] and "player:853x480" in G.GEOMETRY_CHOICES["youtube"]
    assert not any(c.startswith("theater") for c in G.GEOMETRY_CHOICES["twitch"] + G.GEOMETRY_CHOICES["tiktok"])
    run = lambda *a: json.loads(subprocess.run([shutil.which("node"), "-e", THEATER_STUB, B._YT_THEATER_JS, json.dumps(list(a))],
                                               capture_output=True, text=True, check=True).stdout)
    assert run(True, False, True) == {"r": {"action": "clicked"}, "clicks": 1}
    assert run(True, True, True) == {"r": {"action": "already"}, "clicks": 0}           # never toggles it off
    assert run(True, False, False)["r"] == {"action": "no-button"}
    assert run(False, False, True)["r"] == {"action": "no-flexy"}

    class Page:
        def __init__(self):
            self.n = 0
        def eval(self, js):
            self.n += 1
            return json.dumps({"action": "clicked"}) if js == B._YT_THEATER_JS else \
                json.dumps({"on": True, "player": [1818, 812], "dpr": 1})
    r = B.yt_theater(Page(), settle_s=0)
    assert r == {"action": "clicked", "on": True, "player": [1818, 812], "dpr": 1, "player_device_px_h": 812}, r


def test_yt_top_quality():
    import yt_channels as Y
    html = '{"qualityLabel":"144p"},{"qualityLabel":"720p60"},{"qualityLabel":"1080p60"},{"qualityLabel":"1440p60"}'
    assert Y.top_quality(html) == 1440 and Y.top_quality('{"qualityLabel":"720p"}') == 720 and Y.top_quality("") is None


def test_device_scale_variant():
    """yt theater at device scale 1.5 (supervisor 2026-10-18, shagun's audit: theater at DPR 1 left the player 699
    device px tall and yt never played above 720p): the spec parses as theater, launches Chrome with
    --force-device-scale-factor merged into CHROME_EXTRA_ARGS (beside any decode-path args), and enters the rotation."""
    import geometry as G
    assert G.parse("theater:1920x1080@1.5") == ("theater", 1920, 1080) and G.parse("theater:1920x1080") == ("theater", 1920, 1080)
    assert G.device_scale("theater:1920x1080@1.5") == 1.5 and G.device_scale("window:1920x1080") is None and G.device_scale(None) is None
    assert G.chrome_args("theater:1920x1080@1.5") == ["--force-device-scale-factor=1.5"] and G.chrome_args("player:853x480") == []
    try:
        G.device_scale("theater:1920x1080@9")
        raise AssertionError("an out-of-range scale must raise")
    except ValueError:
        pass
    assert G.merge_make_vars([], []) == []
    assert G.merge_make_vars(["CHROME_ENV=X=1"], ["--force-device-scale-factor=1.5"]) == \
        ["CHROME_ENV=X=1", "CHROME_EXTRA_ARGS=--force-device-scale-factor=1.5"]
    assert G.merge_make_vars(["CHROME_EXTRA_ARGS=--ignore-gpu-blocklist"], ["--force-device-scale-factor=1.5"]) == \
        ["CHROME_EXTRA_ARGS=--ignore-gpu-blocklist --force-device-scale-factor=1.5"]
    assert "theater:1920x1080@1.5" in G.GEOMETRY_CHOICES["youtube"]
    import tempfile
    from pathlib import Path as P
    with tempfile.TemporaryDirectory() as d:                                  # the least-collected entry goes next
        for i, g in enumerate(G.GEOMETRY_CHOICES["youtube"][:-1]):
            sd = P(d) / "youtube" / f"yt_{i}"
            sd.mkdir(parents=True)
            (sd / "meta.json").write_text('{"geometry_target": "%s"}' % g)
        assert G.next_geometry(P(d), "youtube") == "theater:1920x1080@1.5"


def main() -> int:
    test_yt_top_quality()
    test_device_scale_variant()
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
    test_captions_off()
    test_theater()
    print("test_geometry_rect: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
