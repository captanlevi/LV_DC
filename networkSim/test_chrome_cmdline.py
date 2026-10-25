#!/usr/bin/env python3
"""session_meta.chrome_cmdline (composite_check FAIL on chaos at 179c870, shagun: Chrome rewrites its process title,
so the browser's /proc/<pid>/cmdline is one space-joined string with a single trailing NUL and a NUL split never found
--remote-debugging-port): the rewritten title is split on whitespace, children (--type=) are excluded, a NUL-separated
cmdline still reads as before. A fake /proc tree.

    python3 test_chrome_cmdline.py        (exit 0 = pass)
"""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.SimpleNamespace(WebSocket=object, WebSocketTimeoutException=Exception)
import session_meta as S  # noqa: E402

BROWSER = ("/opt/google/chrome/chrome --no-sandbox --user-data-dir=/tmp/p --remote-debugging-port=9224 "
           "--disable-backgrounding-occluded-windows --disable-renderer-backgrounding about:blank")
RENDERER = ("/opt/google/chrome/chrome --type=renderer --remote-debugging-port=9224 --lang=en-US")


def _proc(d, entries):
    for pid, raw in entries.items():
        (Path(d) / str(pid)).mkdir()
        (Path(d) / str(pid) / "cmdline").write_bytes(raw)
    (Path(d) / "self").mkdir()
    return d


def main() -> int:
    assert S.proc_argv(BROWSER.encode() + b"\0") == (BROWSER.split(), True)
    assert S.proc_argv(b"a\0b c\0") == (["a", "b c"], False)                  # a real NUL-separated argv is kept
    with tempfile.TemporaryDirectory() as d:                                     # chaos: the rewritten titles
        _proc(d, {100: RENDERER.encode() + b"\0", 200: BROWSER.encode() + b"\0", 300: b"bash\0"})
        r = S.chrome_cmdline("http://localhost:9224", proc_root=d)
        assert r["pid"] == 200 and r["title_rewritten"] is True and r["source"] == "proc scan", r
        assert "--disable-backgrounding-occluded-windows" in r["argv"]
        assert S.chrome_cmdline("http://localhost:9222", proc_root=d)["pid"] is None   # another port: not this one
    with tempfile.TemporaryDirectory() as d:                                     # NUL-separated, as before
        _proc(d, {200: b"\0".join(a.encode() for a in BROWSER.split()) + b"\0"})
        r = S.chrome_cmdline("http://localhost:9224", proc_root=d)
        assert r["pid"] == 200 and r["title_rewritten"] is False, r
    # levi 13:44 (record only): exp reads the browser's argv once it is up as well as at finish (meta chrome_cmdline_start)
    src = (Path(__file__).resolve().parent / "exp.py").read_text()
    # B9 (levi 23:21): read in the join thread (_join) once browser.start() returned
    join = src[src.index("    def _join(browser) -> None:"):src.index("    browser = None\n")]
    i = join.index("chrome_cmdline_start=session_meta.chrome_cmdline()")
    assert join.index("\n            browser.start()\n") < i, i
    # levi 15:42 (record only): the kernel and the VA stack's package versions at session start (meta platform_stack)
    vers = {"intel-media-va-driver-non-free": "24.1.0+dfsg1-1ubuntu0.1", "libva2": "2.20.0-2build1"}
    fake = lambda cmd: types.SimpleNamespace(returncode=0 if cmd[-1] in vers else 1, stdout=vers.get(cmd[-1], ""))
    ps = S.platform_stack(fake)
    assert ps["packages"] == {"intel-media-va-driver-non-free": "24.1.0+dfsg1-1ubuntu0.1", "intel-media-va-driver": None,
                              "libva2": "2.20.0-2build1", "libigdgmm12": None} and ps["kernel_release"], ps
    def boom(cmd):
        raise FileNotFoundError("dpkg-query")
    assert set(S.platform_stack(boom)["errors"]) == set(S.VA_PACKAGES)
    assert "platform_stack=platform_stack()" in (Path(__file__).resolve().parent / "session_meta.py").read_text()
    print("test_chrome_cmdline: ok")
    return 0


def test_chrome_cmdline():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
