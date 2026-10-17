#!/usr/bin/env python3
"""The collector presses play on a player that paused itself during a stall (levi 2026-10-17, chaos tw_10): browser.py's
_RESUME_SELF_PAUSE_JS under node with a stub DOM -- only with the stall label on, the element paused, not ended and
readyState >= 3.

    python3 test_resume.py        (exit 0 = pass)
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
const [stall, paused, rs, ended] = JSON.parse(process.argv[2]);
let played = 0;
const v = {paused, readyState: rs, ended, clientWidth: 800, clientHeight: 450, getClientRects: () => [1],
           play() { played++; return Promise.resolve(); }};
global.document = {documentElement: {dataset: {lvqoeStall: stall ? '1' : '0'}}, querySelectorAll: () => [v]};
const r = eval(process.argv[1]);
console.log(JSON.stringify({r, played}));
"""


def run(stall, paused, rs, ended=False):
    out = subprocess.run([shutil.which("node"), "-e", STUB, B._RESUME_SELF_PAUSE_JS, json.dumps([stall, paused, rs, ended])],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_real_twitch_self_pause_fixture():
    """shagun's capture on chaos tw_12 (2026-10-17): the real player DOM has one <video> (the one the resume JS picks),
    and twitch paused it with readyState 4 while the stall label was off and its own button still read "playing". By
    levi's ruling (resume a self-pause after a stall) the collector leaves that one alone."""
    html = (HERE / "fixtures" / "tw_player_dom_self_pause_2026-10-17.html").read_text()
    assert html.count("<video") == 1 and 'aria-label="Twitch video player"' in html
    st = json.loads((HERE / "fixtures" / "tw_player_state_self_pause_2026-10-17.json").read_text())
    stall = str((st.get("lvqoe") or {}).get("lvqoeStall", "0")) == "1"
    r = run(stall, bool(st["paused"]), int(st["rs"]), bool(st.get("ended")))
    assert r["played"] == 0 and not stall, (st, r)


def main() -> int:
    if not shutil.which("node"):
        print("SKIP: node not found")
        return 0
    assert run(True, True, 4) == {"r": "resumed", "played": 1}
    assert run(True, True, 2)["played"] == 0          # no data yet: the stall goes on
    assert run(False, True, 4)["played"] == 0         # not in a stall: never press play
    assert run(True, False, 4)["played"] == 0         # already playing
    assert run(True, True, 4, ended=True)["played"] == 0
    test_real_twitch_self_pause_fixture()
    print("test_resume: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
