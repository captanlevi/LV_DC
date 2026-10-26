#!/usr/bin/env python3
"""v3 shots (levi 16:57 / 17:45): the cadence is 60 s plus event triggers (start, A1 dims change, STALL onset: content.js;
the error-page onset: PlayerErrorWatch.on_onset -> BrowserSession's queue -> the shooter's `pending`); LVQOE_SHOT_CROP
= player (default) | viewport keeps the whole page area for A7's content-box C5 (both arms), an unknown value falling
back to player and recorded (meta.json shot_crop {crop, env}).

    python3 test_shots_v3.py        (exit 0 = pass)
"""
import importlib
import inspect
import os
import re
import sys
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


def test_cadence_and_crop():
    assert B.SHOT_INTERVAL_S == 60.0 and B.SHOT_CROPS == ("player", "viewport")
    saved = os.environ.get("LVQOE_SHOT_CROP")
    try:
        for env, want in (("viewport", "viewport"), ("player", "player"), ("screen", "viewport"), (None, "viewport")):
            if env is None:
                os.environ.pop("LVQOE_SHOT_CROP", None)
            else:
                os.environ["LVQOE_SHOT_CROP"] = env
            R = importlib.reload(B)
            assert R.SHOT_CROP == want and R.SHOT_CROP_RAW == (env or "viewport"), (env, R.SHOT_CROP)
    finally:
        if saved is None:
            os.environ.pop("LVQOE_SHOT_CROP", None)
        else:
            os.environ["LVQOE_SHOT_CROP"] = saved
        importlib.reload(B)
    src = inspect.getsource(B._composite_shot)
    assert 'if player and SHOT_CROP == "player" else None' in src
    loop = inspect.getsource(B._screenshot_loop)
    assert 'SHOT_CROP == "player"' in loop and "pending" in inspect.signature(B._screenshot_loop).parameters


def test_error_onset_shot():
    w = B.PlayerErrorWatch()
    hits = []
    w.on_onset = hits.append
    err = {"text": "Something went wrong"}
    w.step(err, {"rs": 0}, 100.0)
    w.step(err, {"rs": 0}, 101.0)                     # the same error page: no second onset
    w.step(None, {"rs": 4}, 102.0)
    w.step(err, {"rs": 0}, 103.0)                     # a new onset
    assert hits == [100.0, 103.0], hits
    src = inspect.getsource(B.BrowserSession)
    assert "self.error_watch.on_onset = self._on_error_onset" in src
    assert "self.shot_origin, self._pop_shot_reason)" in src
    b = B.BrowserSession.__new__(B.BrowserSession)
    b._shot_reasons, b.shot_triggers_py = [], {"error_page": 0}
    b._on_error_onset(1.0)
    assert b.shot_triggers_py == {"error_page": 1}                     # brad / levi 18:00: counted in meta
    assert b._pop_shot_reason() == "error_page" and b._pop_shot_reason() is None


_RATE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
let now = 1000;
const ds = {};
const ctx = { console: { log() {} }, performance: { timeOrigin: 0, now: () => now }, setInterval: () => 0,
  setTimeout: () => 0, clearInterval() {}, MutationObserver: class { observe() {} disconnect() {} },
  XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 }, location: { href: "https://www.tiktok.com/@x/live", hostname: "www.tiktok.com" },
  chrome: { runtime: { lastError: null, sendMessage: (m, cb) => cb && cb({ ok: true }) }, storage: { local: { set() {} } } },
  addEventListener() {}, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) };
ctx.window = ctx; ctx.window.top = ctx;
ctx.document = { documentElement: { dataset: ds }, body: {}, visibilityState: "visible", querySelectorAll: () => [],
  querySelector: () => null, evaluate: () => ({ snapshotLength: 0 }) };
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__shots = () => ({ suppressed: shotSuppressed, trig: shotTrig, n: shotReqN });", ctx);
const reqs = [];
for (let i = 0; i < 30; i++) {           // a churn loop: a stall onset every second for 30 s
  ctx.requestShot("stall_onset", i); reqs.push(ds.lvqoeShotReq); now += 1000;
}
ctx.requestShot("ad_start", 99);          // another reason is not held back by stall_onset's gap
fs.writeFileSync(process.argv[3], JSON.stringify({ ...ctx.__shots(), changes: new Set(reqs).size, last: ds.lvqoeShotReq }));
"""


def test_event_shots_are_rate_limited():
    """levi 00:52 / 00:53 (WA2 r1: 69 stall_onset shots in one tt churn period): event shots of one reason >= 10 s
    apart, the suppressed ones counted; Python's error_page trigger the same; the two gaps equal."""
    import json
    import shutil
    import subprocess
    import tempfile
    js = (HERE.parent / "labeling" / "content.js").read_text()
    assert float(re.search(r"const SHOT_EVENT_MIN_GAP_S = ([0-9.]+);", js).group(1)) == B.SHOT_EVENT_MIN_GAP_S == 10.0
    assert shutil.which("node"), "node not found"
    with tempfile.TemporaryDirectory() as td:
        h, out = Path(td) / "h.js", Path(td) / "o.json"
        h.write_text(_RATE_HARNESS)
        subprocess.run(["node", str(h), str(HERE.parent / "labeling" / "content.js"), str(out)], check=True)
        d = json.loads(out.read_text())
    assert d["trig"]["stall_onset"] == 3 and d["suppressed"] == {"stall_onset": 27} and d["changes"] == 3, d
    assert d["trig"]["ad_start"] == 1 and d["last"].split("|")[1] == "ad_start"
    b = B.BrowserSession.__new__(B.BrowserSession)
    b._shot_reasons, b.shot_triggers_py = [], {"error_page": 0}
    for t in (100.0, 101.0, 109.0, 110.0, 125.0):
        b._on_error_onset(t)
    assert b.shot_triggers_py == {"error_page": 3} and b.shot_triggers_py_suppressed == {"error_page": 2}
    assert b._shot_reasons == ["error_page"] * 3


def test_pending_trigger_shoots():
    """The shooter's `pending` (the error-page onset) behaviourally: a content.js request in the same poll wins and the
    Python reason waits for the next poll; a raising `pending` costs no shot and does not stop the loop."""
    import base64
    import io
    import json
    import tempfile
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 36)).save(buf, format="JPEG")
    jpeg = base64.b64encode(buf.getvalue()).decode()

    class Page:
        def eval(self, js):
            if "lvqoeShotReq" in js:                   # the same content.js request on every poll: one event shot
                return json.dumps(["7", "1000", "1|stall_onset|e1"])
            return "null"                              # _PLAYER_RECT_JS

        def call(self, method, params):
            assert method == "Page.captureScreenshot"
            return {"data": jpeg}

    class Polls:                                       # four polls, no real waiting
        n = 0

        def wait(self, timeout=None):
            self.n += 1
            return self.n > 4
    queue, calls = ["error_page"], []

    def pending():
        calls.append(1)
        if len(calls) == 1:                            # poll 2: the queue raises: no shot, the loop goes on
            raise RuntimeError("queue broke")
        return queue.pop(0) if queue else None
    saved = B.SHOT_SOURCE
    with tempfile.TemporaryDirectory() as d:
        try:
            B.SHOT_SOURCE = "cdp"
            B._screenshot_loop(Page(), Path(d), Polls(), pending=pending)
        finally:
            B.SHOT_SOURCE = saved
        rows = [json.loads(x) for x in (Path(d) / "index.jsonl").read_text().splitlines()]
    assert [r["reason"] for r in rows] == ["stall_onset", "error_page"], rows
    assert rows[1]["capture"] == "cdp" and rows[1]["tick"] == "7"
    assert len(calls) == 3                             # poll 1: the request won, pending not asked; polls 2-4 asked


def test_meta_and_refusal():
    src = (HERE / "exp.py").read_text()
    assert "shot_crop=browser.shot_crop," in src and "shot_triggers_py=browser.shot_triggers_py," in src
    # levi 17:56: an unknown value refuses at exp start (named), before anything is collected
    assert 'if os.environ.get("LVQOE_SHOT_CROP") not in (None, "player", "viewport"):' in src
    assert src.index('LVQOE_SHOT_CROP={os.environ') < src.index("session_dir = (make_session_dir(platform)")


def main() -> int:
    test_cadence_and_crop()
    test_error_onset_shot()
    test_event_shots_are_rate_limited()
    test_pending_trigger_shoots()
    test_meta_and_refusal()
    print("test_shots_v3: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
