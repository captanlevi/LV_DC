#!/usr/bin/env python3
"""#8' (levi 88410f0b, record-only; brad on WA3 tw_100003: twitch's "Stream Chat" panel drawn over the right of the
<video> element): every shots/index.jsonl row carries a SHOT_GRID (16 x 9) document.elementFromPoint grid over
player.rect, read in the same eval as player.rect (_SHOT_RECT_JS), as occlusion {grid, cells, legend} plus
video_cell_share. The calibrations' _PLAYER_RECT_JS is unchanged. JS under node with a stub DOM; the writer driven with
a fake CDP page (no browser).

    python3 test_shot_occlusion.py        (exit 0 = pass)
"""
import base64
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    m = types.ModuleType("websocket")
    m.WebSocketTimeoutException = type("WebSocketTimeoutException", (Exception,), {})
    m.WebSocket = object
    sys.modules["websocket"] = m
import browser as B  # noqa: E402

# boxes: [{rect, id, cls, bg, parent: "player" | "root"}], topmost first; the <video> sits in player > container > video
STUB = r"""
const cfg = JSON.parse(process.argv[2]);
const root = {tagName: 'BODY', parentElement: null, className: ''};
const player = {tagName: 'DIV', id: 'player', className: 'video-player', parentElement: root};
const cont = {tagName: 'DIV', className: 'container', parentElement: player};
const box = (r, extra) => Object.assign({getBoundingClientRect: () => ({x: r[0], y: r[1], width: r[2], height: r[3]}),
                                         offsetParent: {}, clientWidth: r[2], clientHeight: r[3]}, extra);
const v = cfg.video ? box(cfg.video, {tagName: 'VIDEO', className: '', parentElement: cont, bgc: 'rgba(0, 0, 0, 0)'}) : null;
const overlays = (cfg.overlays || []).map(o => box(o.rect, {tagName: 'DIV', id: o.id || '', className: o.cls || '',
    parentElement: o.parent === 'player' ? player : root, bgc: o.bg ? 'rgb(24, 24, 27)' : 'rgba(0, 0, 0, 0)'}));
const inside = (e, x, y) => { const r = e.getBoundingClientRect();
                              return x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height; };
global.window = {innerWidth: cfg.inner[0], innerHeight: cfg.inner[1], devicePixelRatio: cfg.dpr};
global.screen = {width: 1920, height: 1080};
global.getComputedStyle = e => ({backgroundColor: e.bgc, backgroundImage: 'none'});
global.document = {
  fullscreenElement: null,
  querySelectorAll: s => s === 'video' && v ? [v] : [],
  elementFromPoint: (x, y) => (cfg.null_rect && inside(box(cfg.null_rect, {}), x, y)) ? null :
      [...overlays, ...(v ? [v] : [])].find(e => inside(e, x, y)) || root,
};
console.log(eval(process.argv[1]));
"""


def run(**cfg) -> dict:
    cfg = {"inner": [1280, 720], "dpr": 1.5, **cfg}
    out = subprocess.run([shutil.which("node"), "-e", STUB, B._SHOT_RECT_JS, json.dumps(cfg)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_no_overlay():
    """the picture unobstructed: all 144 cells on the video, share 1.0, empty legend; player / dpr as before."""
    r = run(video=[50, 100, 863, 485])
    assert B.SHOT_GRID == (16, 9)
    occ = r["occlusion"]
    assert occ["grid"] == [16, 9] and occ["cells"] == "v" * 144 and occ["legend"] == {} and occ["share"] == 1.0, occ
    assert r["player"] == {"rect": [50, 100, 863, 485], "inner": [1280, 720], "dpr": 1.5} and r["dpr"] == 1.5


def test_chat_overlay():
    """tw_100003's shape: a chat panel over the element's right part (x >= 575 of 50..913) flags those cells with its
    identifier, opaque and outside the player (up = 3: video -> container -> player -> body); share = the rest."""
    r = run(video=[50, 100, 863, 485],
            overlays=[{"rect": [575, 0, 705, 720], "id": "chat-room", "cls": "stream-chat  chat-shell extra", "bg": True}])
    occ = r["occlusion"]
    cw = 863 / 16
    on_cols = sum(1 for i in range(16) if 50 + (i + 0.5) * cw < 575)                 # 10 columns left of the panel
    assert on_cols == 10
    assert occ["cells"] == ("v" * on_cols + "a" * (16 - on_cols)) * 9, occ["cells"]
    assert occ["legend"] == {"a": {"id": "div#chat-room.stream-chat.chat-shell", "up": 3, "bg": True}}, occ["legend"]
    assert occ["share"] == round(on_cols * 9 / 144, 4)


def test_transparent_player_overlay_and_viewport_edge():
    """a transparent click overlay inside the player (up = 2: video -> container -> player; bg False) is a distinct legend entry; cells whose centre
    falls outside the viewport are "o"; a point with no element is "n"; no <video>: occlusion null."""
    r = run(video=[0, 600, 640, 360], overlays=[{"rect": [0, 600, 320, 120], "cls": "click-handler", "parent": "player"}])
    occ = r["occlusion"]
    rows = [occ["cells"][16 * j:16 * (j + 1)] for j in range(9)]
    # the viewport ends at y = 720: rows with centre 600 + (j + 0.5) * 40 < 720 are j = 0..2
    assert rows[:3] == ["a" * 8 + "v" * 8] * 3 and rows[3:] == ["o" * 16] * 6, rows
    assert occ["legend"]["a"] == {"id": "div.click-handler", "up": 2, "bg": False}
    assert occ["share"] == round(24 / 144, 4)
    assert run()["occlusion"] is None and run()["player"] is None
    # elementFromPoint returning null over the video's box: "n", not on the video, no legend entry
    r = run(video=[0, 0, 640, 360], null_rect=[0, 0, 640, 40])
    assert r["occlusion"]["cells"] == "n" * 16 + "v" * 128 and r["occlusion"]["legend"] == {}
    assert r["occlusion"]["share"] == round(128 / 144, 4)


def test_plain_rect_js_unchanged():
    """the calibrations' eval keeps no grid; the shot loop evals the grid variant."""
    assert "elementFromPoint" not in B._PLAYER_RECT_JS and "GRID" not in B._PLAYER_RECT_JS
    assert "elementFromPoint" in B._SHOT_RECT_JS and "GRID" not in B._SHOT_RECT_JS
    import inspect
    loop = inspect.getsource(B._screenshot_loop)
    assert "page.eval(_SHOT_RECT_JS)" in loop and "page.eval(_PLAYER_RECT_JS)" not in loop


class _Page:
    """content.js has raised one shot request; the Python side queues one error_page onset."""
    def __init__(self, occlusion):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (1280, 720), (90, 90, 90)).save(buf, format="JPEG")
        self.jpg, self.occ = base64.b64encode(buf.getvalue()).decode(), occlusion

    def eval(self, js, await_promise=False):
        if js is B._SHOT_RECT_JS:
            return json.dumps({"player": {"rect": [50, 100, 863, 485], "inner": [1280, 720], "dpr": 1.5},
                               "viewport": [1280, 720], "dpr": 1.5, "fullscreen": False, "screen": [1920, 1080],
                               "occlusion": json.loads(json.dumps(self.occ))})
        if "lvqoeShotReq" in js:
            return json.dumps(["13", "1792862643.1", "1|tier_change|13"])
        return "null"

    def call(self, method, params=None):
        return {"data": self.jpg} if method == "Page.captureScreenshot" else {}


def test_writer_rows():
    """both triggers (content.js's request, the Python error_page onset) write occlusion and video_cell_share; share
    lives on the row, not inside occlusion; a grid that threw leaves player intact and the share null."""
    occ = {"grid": [16, 9], "cells": ("v" * 10 + "a" * 6) * 9,
           "legend": {"a": {"id": "div#chat-room.stream-chat", "up": 3, "bg": True}}, "share": 0.625}
    for o, share in ((occ, 0.625), ({"error": "TypeError: x"}, None)):
        real = B.SHOT_SOURCE
        B.SHOT_SOURCE = "cdp"
        q = ["error_page"]
        try:
            with tempfile.TemporaryDirectory() as d:
                stop = threading.Event()
                idx = Path(d) / "index.jsonl"
                t = threading.Thread(target=B._screenshot_loop, args=(_Page(o), Path(d), stop),
                                     kwargs={"pending": lambda: q.pop() if q else None}, daemon=True)
                t.start()
                for _ in range(100):
                    if idx.exists() and len(idx.read_text().splitlines()) >= 2:
                        break
                    threading.Event().wait(0.1)
                stop.set()
                t.join(5)
                rows = [json.loads(x) for x in idx.read_text().splitlines()]
        finally:
            B.SHOT_SOURCE = real
        assert [x["reason"] for x in rows[:2]] == ["tier_change", "error_page"], rows
        for row in rows[:2]:
            assert row["video_cell_share"] == share, row
            want = {k: v for k, v in o.items() if k != "share"}
            assert row["occlusion"] == want, row["occlusion"]
            assert row["player"]["rect"] == [50, 100, 863, 485] and row["viewport"]["dpr"] == 1.5


def main() -> int:
    if not shutil.which("node"):
        print("test_shot_occlusion: node missing")
        return 1
    test_no_overlay()
    test_chat_overlay()
    test_transparent_player_overlay_and_viewport_edge()
    test_plain_rect_js_unchanged()
    test_writer_rows()
    print("test_shot_occlusion: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
