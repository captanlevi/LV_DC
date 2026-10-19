#!/usr/bin/env python3
"""ops/diag/tt_room_dom.py (supervisor 2026-10-19, ti_100010): the trio / N-slot tt sessions the slots log shows running
are sampled on their own slot's Chrome (meta.json concurrency.cdp_port), each record carrying the port; finished,
other-platform and superseded sessions are not; single mode still reads the batch Chrome's base. No browser: CDP is
stubbed.

    python3 test_tt_room_dom.py        (exit 0 = pass)
"""
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

try:
    import websocket  # noqa: F401
except ImportError:                               # no browser needed: CDP is stubbed below
    sys.modules["websocket"] = types.ModuleType("websocket")
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tt_room_dom", HERE.parent / "ops" / "diag" / "tt_room_dom.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def _sess(troot: Path, name: str, port: int, finished: bool = False, platform: str = "tiktok") -> Path:
    sd = troot / platform / name
    sd.mkdir(parents=True)
    (sd / "meta.json").write_text(json.dumps({"concurrency": {"cdp_port": port, "tag": "tt1"}}))
    if finished:
        (sd / "check.md").write_text("done")
    return sd


def test_running_trio_tt_sessions_and_their_ports():
    with tempfile.TemporaryDirectory() as d:
        troot = Path(d) / "_trio"
        _sess(troot, "ti_100001", 9225)                                  # superseded on its slot
        live = _sess(troot, "ti_100002", 9225)
        live2 = _sess(troot, "ti_100003", 9227)                          # N = 4: a second tt slot
        _sess(troot, "ti_100004", 9228, finished=True)                   # ended without an end event: check.md
        _sess(troot, "tw_100005", 9224, platform="twitch")
        ev = [{"event": "start", "slot": "tt1", "session": "ti_100001", "platform": "tiktok"},
              {"event": "start", "slot": "tt1", "session": "ti_100002", "platform": "tiktok"},
              {"event": "start", "slot": "tt2", "session": "ti_100003", "platform": "tiktok"},
              {"event": "start", "slot": "tt3", "session": "ti_100004", "platform": "tiktok"},
              {"event": "start", "slot": "tw1", "session": "tw_100005", "platform": "twitch"},
              {"event": "start", "slot": "tt4", "session": "ti_100006", "platform": "tiktok"},
              {"event": "end", "slot": "tt4", "session": "ti_100006"}]
        (troot / "_summaries").mkdir(parents=True)
        (troot / R.T.SLOTS_LOG).write_text("".join(json.dumps(e) + "\n" for e in ev) + "not json\n")
        got = R.running_trio_tt(troot)
        assert sorted((s.name, p) for s, p in got) == [("ti_100002", 9225), ("ti_100003", 9227)], got
        bases = []

        class Page:
            def __init__(self, ws):
                bases.append(R.B.CDP_BASE)

            def eval(self, js):
                return json.dumps({"url": "https://www.tiktok.com/@x/live", "videos": [[537, 676, 4]]})

            def close(self):
                pass
        saved = (R.B.CDPPage, R.B._get_page_ws_url, R.B.CDP_BASE)
        R.B.CDPPage, R.B._get_page_ws_url = Page, lambda: "ws://stub"
        try:
            for sd, port in got:
                R.sample(sd, f"http://localhost:{port}", port)
        finally:
            R.B.CDPPage, R.B._get_page_ws_url, R.B.CDP_BASE = saved
        assert bases == ["http://localhost:9225", "http://localhost:9227"], bases
        rec = json.loads((live / "tt_room_dom.jsonl").read_text())
        assert rec["cdp_port"] == 9225 and rec["videos"] == [[537, 676, 4]] and "ts" in rec
        assert json.loads((live2 / "tt_room_dom.jsonl").read_text())["cdp_port"] == 9227
        assert R.running_trio_tt(Path(d) / "nothing") == []
    assert R.SINGLE_BASE == R.B.CDP_BASE                                  # single mode: the batch Chrome's base


def main() -> int:
    test_running_trio_tt_sessions_and_their_ports()
    print("test_tt_room_dom: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
