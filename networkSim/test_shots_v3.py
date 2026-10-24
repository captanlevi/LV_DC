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
        for env, want in (("viewport", "viewport"), ("screen", "player"), (None, "player")):
            if env is None:
                os.environ.pop("LVQOE_SHOT_CROP", None)
            else:
                os.environ["LVQOE_SHOT_CROP"] = env
            R = importlib.reload(B)
            assert R.SHOT_CROP == want and R.SHOT_CROP_RAW == (env or "player"), (env, R.SHOT_CROP)
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


def test_meta_and_refusal():
    src = (HERE / "exp.py").read_text()
    assert "shot_crop=browser.shot_crop," in src and "shot_triggers_py=browser.shot_triggers_py," in src
    # levi 17:56: an unknown value refuses at exp start (named), before anything is collected
    assert 'if os.environ.get("LVQOE_SHOT_CROP") not in (None, "player", "viewport"):' in src
    assert src.index('LVQOE_SHOT_CROP={os.environ') < src.index("session_dir = (make_session_dir(platform)")


def main() -> int:
    test_cadence_and_crop()
    test_error_onset_shot()
    test_meta_and_refusal()
    print("test_shots_v3: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
