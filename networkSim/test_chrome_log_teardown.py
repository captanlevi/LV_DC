#!/usr/bin/env python3
"""levi 12:19 (f) (WC3b / WC3c: 0 of 21 sessions kept chrome_stderr.log; meta.chrome_log FileNotFoundError on every one):
kill_chrome removed the slot's stderr log (the Makefile recipe's rm and exp.kill_chrome's unlink) before exp.py's copy.
Run end to end: exp.py's own run / kill_chrome / copy_chrome_log and its teardown lines (taken from its source; exp.py
cannot be imported off a collector), the real Makefile `kill_chrome` recipe against a live fake Chrome on its own CDP
port, for a normal end, a JoinTimeout abort and a discovery raise (all three reach the session's one `finally`). The
session dir must hold a non-empty chrome_stderr.log and meta.chrome_log no error."""
from __future__ import annotations

import ast
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
EXP = (HERE / "exp.py").read_text()
TEARDOWN_START = "        session_meta.finish(session_dir, channel_url, **meta_extra)"
TEARDOWN_END = "        if SLOT is not None:   # trio: in-flight packets land"


def _exp_defs(names: set[str]) -> str:
    """exp.py's own top-level defs / assignments named in `names`, as source."""
    tree = ast.parse(EXP)
    out = []
    for node in tree.body:
        n = getattr(node, "name", None) or (node.targets[0].id if isinstance(node, ast.Assign)
                                            and isinstance(node.targets[0], ast.Name) else None)
        if n in names:
            out.append(ast.get_source_segment(EXP, node))
    assert len(out) == len(names), (names, len(out))
    return "\n\n".join(out)


DEFS = {"run", "CHROME_LOG_COPY", "CHROME_LOG_MTIME_SLACK_S", "copy_chrome_log", "kill_chrome"}


def _teardown() -> str:
    main = EXP[EXP.index('if __name__ == "__main__":'):]
    fin = main.index("    finally:\n        # finalising")
    a, b = main.index(TEARDOWN_START), main.index(TEARDOWN_END)
    assert main.index("    except Exception as e:\n        print(f\"[error] session failed") < fin < a < b
    return textwrap.dedent(main[a:b])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("end", ["normal", "join_timeout at state 7 (21 s)", "error: RuntimeError: discovery"])
def test_the_session_keeps_chrome_stderr(end):
    if not shutil.which("make") or not shutil.which("pkill"):
        pytest.skip("make / pkill absent")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "networkSim").mkdir()
        (root / "current_data").mkdir()
        shutil.copy(HERE / "Makefile", root / "networkSim" / "Makefile")
        session_dir = root / "S" / "ti_100003"
        session_dir.mkdir(parents=True)
        port, tag = _free_port(), "zt"
        log = root / "current_data" / f"chrome_{tag}.log"
        (root / "current_data" / f"chrome_{tag}.pid").write_text("1")
        with open(log, "w") as fh:                    # a live "Chrome" on its own port, writing its stderr
            fake = subprocess.Popen([sys.executable, "-c", "import sys, time\nfor i in range(6000):\n"
                                     "    print(f'[chrome] stderr line {i}', file=sys.stderr, flush=True)\n"
                                     "    time.sleep(0.02)", f"--remote-debugging-port={port}"], stderr=fh)
        time.sleep(0.5)
        written = []
        ns = {"__file__": str(root / "networkSim" / "exp.py"), "Path": Path, "os": os, "signal": signal,
              "subprocess": subprocess, "shutil": shutil, "SLOT": types.SimpleNamespace(tag=tag),
              "SLOT_VARS": [f"CDP_PORT={port}", f"SLOT_SUFFIX=_{tag}"]}
        exec(_exp_defs(DEFS), ns)
        ns.update(session_dir=session_dir, channel_url=None, meta_extra={"abort_reason": None if end == "normal" else end},
                  _t_chrome_launch=time.time() - 1.0,
                  chrome_profile=root / "P",
                  session_meta=types.SimpleNamespace(finish=lambda *a, **k: None,
                                                     write=lambda sd, **k: written.append(k)))
        cwd = os.getcwd()
        try:
            os.chdir(root / "networkSim")             # exp.py runs make from networkSim
            exec(_teardown(), ns)
        finally:
            os.chdir(cwd)
            if fake.poll() is None:
                fake.kill()
        assert fake.wait(timeout=15) is not None      # the recipe killed it
        kept = session_dir / "chrome_stderr.log"
        assert kept.exists() and "[chrome] stderr line" in kept.read_text(), end
        assert kept.read_text() == log.read_text()     # copied after the kill: Chrome's last lines are in it (brad)
        rec = next(w["chrome_log"] for w in written if "chrome_log" in w)
        assert "error" not in rec and rec["bytes"] > 0, rec
        assert not (root / "current_data" / f"chrome_{tag}.pid").exists()     # the pid file still goes


def test_a_launch_refused_before_chrome_never_copies_the_previous_log():
    """brad 734f961d / levi 12:50: kill_chrome keeps the log, so a launch refused before start_chrome's > (a geometry
    refusal) leaves the previous session's; exp.py's pre-launch-failure lines, run from source, record it stale and copy
    nothing."""
    main = EXP[EXP.index('if __name__ == "__main__":'):]
    a = main.index("    _t_chrome_launch = time.time()")
    b = main.index('        raise ValueError("Cannot open chrome with SSL key log enabled")')
    block = textwrap.dedent(main[a:b]) + "    pass\n"
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "networkSim").mkdir()
        (root / "current_data").mkdir()
        old = root / "current_data" / "chrome_zt.log"
        old.write_text("[chrome] the previous session's stderr\n")
        os.utime(old, (time.time() - 120, time.time() - 120))
        session_dir = root / "S" / "yt_100004"
        session_dir.mkdir(parents=True)
        written, undone = [], []
        ns = {"__file__": str(root / "networkSim" / "exp.py"), "Path": Path, "os": os, "signal": signal,
              "subprocess": subprocess, "shutil": shutil, "time": time, "SLOT": types.SimpleNamespace(tag="zt"),
              "session_dir": session_dir, "chrome_profile": root / "P", "platform": "youtube", "WINDOW_SIZES": {},
              "args": types.SimpleNamespace(geometry="bad"),
              "open_chrome_with_ssl_key_log": lambda *a, **k: False,       # geometry refused before make start_chrome
              "is_capture": True, "dns_cap": None, "tcp_def_caps": [],
              "startup_undo": lambda *a, **k: undone.append((a, k)) or {},  # codex triage (b): stops what started
              "session_meta": types.SimpleNamespace(write=lambda sd, **k: written.append(k))}
        exec(_exp_defs(DEFS), ns)
        exec(block, ns)
        assert not (session_dir / "chrome_stderr.log").exists()
        rec = written[0]["chrome_log"]
        assert rec["stale"] is True and rec["mtime"] < rec["since"], rec
        assert undone == [(([None],), {"capture": True, "chrome_profile": root / "P"})]   # after the copy, before the raise


def test_nothing_removes_the_chrome_log_before_the_copy():
    """Static guard: neither the recipe nor exp.kill_chrome removes the stderr log; start_chrome's > truncates it."""
    mk = (HERE / "Makefile").read_text()
    recipe = mk[mk.index("\nkill_chrome:"):]
    recipe = recipe[:recipe.index("\n\n")]
    assert "rm -f $(CAPTURE_DIR)/chrome$(SLOT_SUFFIX).pid\n" in recipe + "\n"
    assert "chrome$(SLOT_SUFFIX).log" not in recipe and "$(CHROME_LOG)" not in recipe
    assert "> $(CHROME_LOG)" in mk[mk.index("\nstart_chrome:"):mk.index("\nkill_chrome:")]
    kc = _exp_defs({"kill_chrome"})
    assert ".log" not in kc.split('"""')[-1], kc


def main() -> int:
    for end in ["normal", "join_timeout at state 7 (21 s)", "error: RuntimeError: discovery"]:
        test_the_session_keeps_chrome_stderr(end)
    test_a_launch_refused_before_chrome_never_copies_the_previous_log()
    test_nothing_removes_the_chrome_log_before_the_copy()
    print("test_chrome_log_teardown: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
