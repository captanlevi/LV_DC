#!/usr/bin/env python3
"""v3 t70 (t67 A16 / A17 / A8): the session-start records.
  - A16: trio_slots.client_addr reads the collection interface's IPv4 address(es) (`ip -4 -o addr`), the first is
    meta.concurrency.client_addr (what the W4 loss reader takes before any inference);
  - A17: the default class's TCP headers are captured on both mirrors (lvi_def / lvo_def), snaplen 128, with the
    session captures' tcpdump options, and the files join <session>/trio/ with their sizes in meta;
  - A8: browser._SCREEN_JS (under node) reports the screen and the rAF-measured refresh: 60 Hz pacing reads 60, 30 Hz 30.

    python3 test_v3_session_records.py        (needs node for A8; exit 0 = pass)
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
import trio_slots as T  # noqa: E402

IP_OUT = ("3: wlp0s20f3    inet 192.168.4.50/24 brd 192.168.4.255 scope global dynamic noprefixroute wlp0s20f3\\"
          "       valid_lft 85000sec preferred_lft 85000sec\n"
          "3: wlp0s20f3    inet 10.9.0.2/32 scope global wlp0s20f3\\       valid_lft forever preferred_lft forever\n")


def test_client_addr():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(stdout=IP_OUT, returncode=0)
    r = T.client_addr("wlp0s20f3", run=run)
    assert calls == [["ip", "-4", "-o", "addr", "show", "dev", "wlp0s20f3"]]
    assert r == {"iface": "wlp0s20f3", "addr": "192.168.4.50", "addrs": ["192.168.4.50", "10.9.0.2"]}, r
    assert T.client_addr("x", run=lambda *a, **k: types.SimpleNamespace(stdout="", returncode=1))["addr"] is None

    def boom(*a, **k):
        raise OSError("no ip")
    r = T.client_addr("x", run=boom)
    assert r["addr"] is None and "no ip" in r["error"]


def test_default_tcp_capture(monkeypatch=None):
    made = []

    class P:
        def __init__(self, cmd):
            made.append(cmd)
    saved = T.subprocess.Popen
    T.subprocess.Popen = P
    try:
        ps = T.default_tcp_capture_start(Path("/S"))
    finally:
        T.subprocess.Popen = saved
    assert len(ps) == 2 and all(c[:3] == ["sudo", "-n", "sh"] for c in made)
    for c, dev, side in zip(made, ("lvi_def", "lvo_def"), ("in", "out")):
        sh = c[-1]
        assert sh.startswith("exec tcpdump -Z root -B 65536 -s 128 -i " + dev + " -U -w - \"tcp\""), sh
        assert f'> "/S/default_tcp_{side}.pcap" 2> "/S/default_tcp_{side}.err"' in sh
    assert T.DEFAULT_TCP_SNAPLEN == 128
    src = (HERE / "exp.py").read_text()
    assert "tcp_def_caps = trio_slots.default_tcp_capture_start(staging)" in src
    assert 'list(staging.glob("default_tcp_*"))' in src and 'TRIO["default_tcp"] = {"snaplen"' in src
    assert 'TRIO.update(client_addr=_ca["addr"], client_iface=_ca)' in src and "screen=browser.screen" in src


HARNESS = r"""
const vm = require("vm");
const step = Number(process.argv[2]);
let now = 0;
const q = [];
const ctx = { screen: { width: 1920, height: 1080, availWidth: 1850, availHeight: 1053, colorDepth: 24 },
              window: { devicePixelRatio: 1 }, document: { visibilityState: "visible" },
              requestAnimationFrame: (f) => q.push(f), setTimeout: () => 0, Math, JSON, Promise };
vm.createContext(ctx);
const p = vm.runInContext(process.argv[1], ctx);
(async () => { while (q.length) { now += step; q.shift()(now); } console.log(await p); })();
"""


def test_screen_js():
    node = shutil.which("node")
    assert node, "node not found: the A8 screen read cannot be tested (install node)"
    for step, hz in ((1000 / 60, 60.0), (1000 / 30, 30.0)):
        out = subprocess.run([node, "-e", HARNESS, B._SCREEN_JS, str(step)], capture_output=True, text=True, check=True)
        r = json.loads(out.stdout)
        assert r["refresh_hz"] == hz and r["raf_frames"] == B.SCREEN_RAF_FRAMES + 1, r
        assert (r["w"], r["h"], r["avail_w"], r["avail_h"], r["dpr"]) == (1920, 1080, 1850, 1053, 1)


def main() -> int:
    test_client_addr()
    test_default_tcp_capture()
    test_screen_js()
    print("test_v3_session_records: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
