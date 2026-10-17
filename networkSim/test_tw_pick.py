#!/usr/bin/env python3
"""twitch directory pick (supervisor 2026-10-17, tw_7 / tw_8 both on the Just Chatting top card): recent channels from
the last 10 twitch sessions' meta.json, and the page-side pick skips them and clicks the first other card (node, stub
DOM). No browser needed.

    python3 test_tw_pick.py        (exit 0 = pass)
"""
import json
import shutil
import subprocess
import sys
import tempfile
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
const clicked = [];
const mk = (href, vis = true) => ({href, offsetParent: vis ? {} : null, click() { clicked.push(href); }});
const links = [mk('https://www.twitch.tv/caseoh_'), mk('https://www.twitch.tv/caseoh_'), mk('https://www.twitch.tv/directory/game/X'),
               mk('https://www.twitch.tv/hidden', false), mk('https://www.twitch.tv/Second/'), mk('https://www.twitch.tv/third')];
global.document = {querySelectorAll: () => links};
const out = JSON.parse(eval(process.argv[1]));
console.log(JSON.stringify({out, clicked}));
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "data"
        for i, (where, url) in enumerate([("twitch/tw_7", "https://www.twitch.tv/caseoh_"),
                                          ("_failed/twitch/tw_8", "https://www.twitch.tv/caseoh_?x=1"),
                                          ("twitch/tw_1", "https://www.twitch.tv/directory/game/Just%20Chatting")]):
            d = root / where
            d.mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps({"channel_url": url, "started_at": 100 + i}))
        cur = root / "twitch" / "tw_9"
        cur.mkdir(parents=True)
        recent = B.recent_tw_channels(cur)
        assert recent == ["https://www.twitch.tv/caseoh_"], recent
    node = shutil.which("node")
    if not node:
        print("SKIP js: node not found")
        return 0
    r = subprocess.run([node, "-e", STUB, B._pick_twitch_js(recent)], capture_output=True, text=True, check=True)
    res = json.loads(r.stdout)
    assert res["out"]["pick"] == "https://www.twitch.tv/second", res
    assert res["clicked"] == ["https://www.twitch.tv/Second/"], res
    assert res["out"]["candidates"] == ["https://www.twitch.tv/caseoh_", "https://www.twitch.tv/second", "https://www.twitch.tv/third"], res
    r = subprocess.run([node, "-e", STUB, B._pick_twitch_js([])], capture_output=True, text=True, check=True)
    assert json.loads(r.stdout)["out"]["pick"] == "https://www.twitch.tv/caseoh_"
    print("test_tw_pick: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
