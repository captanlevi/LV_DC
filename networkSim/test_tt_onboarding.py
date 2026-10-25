#!/usr/bin/env python3
"""batch #10 (levi 03:29): TikTok's feed-navigation guide ("Scroll, use the up / down keys, or click the arrows on the
right to browse your feed." + "Got it") covered the video in WA3 tt sessions' first shots. The tt action JS clicks its own
"Got it" only when the guide's text is that guide's, at most 3 times (then tt_onboarding_stuck, no click), and every
action is recorded (meta.json tt_onboarding). JS under node with a stub DOM.

    python3 test_tt_onboarding.py        (exit 0 = pass)
"""
import json
import shutil
import subprocess
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

STUB = r"""
const cfg = JSON.parse(process.argv[2]);
const ds = {};
let clicks = 0;
const host = {textContent: cfg.text, parentElement: null};
const mid = {textContent: 'Got it', parentElement: host};
const btn = {textContent: cfg.label, parentElement: mid, offsetParent: cfg.visible ? {} : null,
             getClientRects: () => cfg.visible ? [1] : [], click() { clicks++; if (cfg.goes) cfg.present = false; }};
global.document = {documentElement: {dataset: ds},
                   querySelectorAll: () => cfg.present ? [btn] : []};
const out = [];
for (let i = 0; i < cfg.calls; i++) out.push(eval(process.argv[1]));
console.log(JSON.stringify({out, clicks}));
"""
GUIDE = "Scroll, use the ↑↓ keys, or click the arrows on the right to browse your feed. Got it"


def run(**cfg):
    cfg = {"text": GUIDE, "label": "Got it", "visible": True, "present": True, "goes": True, "calls": 3, **cfg}
    r = subprocess.run([shutil.which("node"), "-e", STUB, B._SKIP_AD_JS["tiktok"], json.dumps(cfg)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def test_the_guide_is_dismissed_once_and_recorded():
    r = run()
    assert r["clicks"] == 1 and r["out"][0].startswith("tt_onboarding_dismissed [Scroll, use the") and r["out"][1:] == [None, None]


def test_a_guide_that_stays_is_clicked_three_times_then_reported_stuck():
    r = run(goes=False, calls=5)
    assert r["clicks"] == 3 and all(x.startswith("tt_onboarding_dismissed") for x in r["out"][:3])
    assert all(x.startswith("tt_onboarding_stuck") for x in r["out"][3:])


def test_another_got_it_dialog_is_never_clicked():
    r = run(text="Confirm your age to continue. Got it")
    assert r["clicks"] == 0 and r["out"] == [None, None, None]
    r = run(label="Got it now", calls=1)
    assert r["clicks"] == 0 and r["out"] == [None]
    r = run(visible=False, calls=1)
    assert r["clicks"] == 0 and r["out"] == [None]


def test_the_session_record():
    class S:
        tt_onboarding = []
    B.BrowserSession._on_ad_action(S, "tt_onboarding_dismissed [Scroll ... browse your feed. Got it]", 12.3456)
    B.BrowserSession._on_ad_action(S, "skipped [yt_ad_showing]", 13.0)
    assert S.tt_onboarding == [{"t": 12.346, "action": "tt_onboarding_dismissed", "text": "Scroll ... browse your feed. Got it"}]
    src = (HERE / "browser.py").read_text()
    assert "self._clock_sample, self._on_ad_action)," in src and "on_action(str(result), time.time())" in src
    assert "tt_onboarding=(browser.tt_onboarding if browser.platform == \"tiktok\" else None)" in (HERE / "exp.py").read_text()


def main() -> int:
    test_the_guide_is_dismissed_once_and_recorded()
    test_a_guide_that_stays_is_clicked_three_times_then_reported_stuck()
    test_another_got_it_dialog_is_never_clicked()
    test_the_session_record()
    print("test_tt_onboarding: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
