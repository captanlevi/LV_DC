#!/usr/bin/env python3
"""t102 #4 (levi 00402 / 00403, spec t102_4_liveness_spec_v1): the browser-liveness abort.

- liveness.probe on a fake /proc and kill: alive, gone, zombie, reused (cmdline without our port), another user's pid;
- LivenessWatch with a fake probe and clock: no fire on the 1st miss, fire at the 2nd (first_miss_t = the 1st, fired_t
  = the 2nd), a miss then alive resets (no sign kept), an unverified pid never arms and never fires, once only;
  stopped = teardown: nothing fires and no lost socket is recorded; the poll thread fires through on_fire;
- the renderer crash (levi 00403 (2)): browser.RendererCrashWatch fires on Inspector.targetCrashed without a sessionId
  (the main page) and never on a child session's (OOPIF / ad iframe / worker), counted; through LivenessWatch: miss_kind
  "renderer", one fire between the two paths; a broken socket is cdp_lost_t, a record only;
- exit code and accounting: exp.py maps browser_dead to 16; orchestrate._account counts 16 toward the slot's failures and
  never marks the channel; 3 / 5 / 6 / 7 / 12 / 13 keep their codes;
- off: BROWSER_LIVENESS_ABORT False -> exp.py arms nothing and browser.py starts no renderer watch (source pins).

    python3 test_liveness.py        (exit 0 = pass)
"""
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import liveness as L  # noqa: E402

PORT = 9224


def _proc(tmp: Path, pid: int, state: str = "S", port: int = PORT) -> Path:
    d = tmp / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_text(f"{pid} (chrome (x)) {state} 1 2 3")       # a ')' inside comm: the last one ends it
    (d / "cmdline").write_bytes(b"/opt/google/chrome/chrome\0--remote-debugging-port=%d\0about:blank\0" % port)
    return tmp


def test_probe(tmp_path):
    alive = lambda pid, sig: None                                                   # noqa: E731

    def gone(pid, sig):
        raise ProcessLookupError

    def other_user(pid, sig):
        raise PermissionError
    proc = _proc(tmp_path, 11)
    assert L.probe(11, PORT, proc, alive) is None
    assert L.probe(11, PORT, proc, gone) == "gone"
    assert L.probe(11, PORT, proc, other_user) is None                             # exists; the cmdline decides
    assert L.probe(11, PORT + 1, proc, alive) == "reused"                           # another slot's port
    _proc(tmp_path, 12, state="Z")
    assert L.probe(12, PORT, proc, alive) == "zombie"
    assert L.probe(13, PORT, proc, alive) == "gone"                                 # no /proc entry: raced away


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _watch(seq, clock=None, pid=42):
    fired = []
    it = iter(seq)
    w = L.LivenessWatch(lambda: pid, PORT, fired.append, probe_fn=lambda p: next(it), clock=clock or _Clock())
    return w, fired


def test_two_misses_fire_once():
    c = _Clock()
    w, fired = _watch([None, None, "gone", "gone", "gone"], c)
    assert w.arm() and w.rec["armed"] and w.rec["pid"] == 42
    c.t = 1005.0
    assert not w.poll() and w.rec["first_miss_t"] is None
    c.t = 1010.0
    assert not w.poll() and w.rec["first_miss_t"] == 1010.0 and w.rec["miss_kind"] == "gone" and fired == []
    c.t = 1015.0
    assert w.poll() and fired == [1015.0] and w.rec["fired_t"] == 1015.0 and w.rec["first_miss_t"] == 1010.0
    assert not w.poll() and fired == [1015.0]                                        # once only
    assert w.rec["polls"] == 3


def test_a_recovered_miss_is_no_sign():
    w, fired = _watch([None, "zombie", None, "gone", None])
    assert w.arm()
    for _ in range(4):
        assert not w.poll()
    assert fired == [] and w.rec["first_miss_t"] is None and w.rec["miss_kind"] is None and w.rec["fired_t"] is None


def test_unverified_pid_never_arms():
    for seq, pid, why in (([], None, "no pid file"), (["gone"], 7, "pid gone at arming"),
                          (["reused"], 7, "pid reused at arming")):
        w, fired = _watch(seq + ["gone"] * 5, pid=pid)
        assert not w.arm() and w.rec["why"] == why and not w.rec["armed"]
        for _ in range(4):
            assert not w.poll()
        assert fired == [] and w.rec["polls"] == 0
        w.start()                                                                    # start() on an unarmed pid: no thread
        assert w._t is None and w.stop()["fired_t"] is None


def test_stopped_never_fires_and_records_no_loss():
    w, fired = _watch([None, "gone", "gone"])
    assert w.arm()
    w._stop.set()                                                                    # teardown began (exp.py stops it first)
    w.poll(), w.poll()
    w.renderer_crashed(1.0)
    w.cdp_lost(2.0)
    assert fired == [] and w.rec["fired_t"] is None and w.rec["cdp_lost_t"] is None


def test_thread_fires_through_on_fire():
    done = threading.Event()
    seq = iter([None] + ["gone"] * 50)
    w = L.LivenessWatch(lambda: 5, PORT, lambda t: done.set(), probe_fn=lambda p: next(seq), poll_s=0.01)
    w.start()
    assert done.wait(2.0)
    rec = w.stop()
    assert rec["fired_t"] is not None and rec["miss_kind"] == "gone" and rec["polls"] == 2


def test_renderer_crash_main_page_only():
    import browser as B
    got = []
    r = B.RendererCrashWatch("ws://unused", got.append)
    child = {"method": "Inspector.targetCrashed", "params": {}, "sessionId": "OOPIF-1"}
    assert not r.handle(child, 10.0) and not r.handle({**child, "sessionId": "W-2"}, 11.0)   # ad iframe / worker
    assert not r.handle({"method": "Inspector.detached", "params": {"reason": "x"}}, 12.0)
    assert got == [] and r.child_crashes == 2
    assert r.handle({"method": "Inspector.targetCrashed", "params": {}}, 13.0) and got == [13.0]
    assert not r.handle({"method": "Inspector.targetCrashed", "params": {}}, 14.0) and got == [13.0]
    assert r.stop() == {"crashed_t": 13.0, "child_crashes": 2, "error": None}


def test_renderer_crash_through_the_watch():
    w, fired = _watch([None, None, "gone", "gone"])
    assert w.arm()
    w.renderer_crashed(1234.5)
    assert fired == [1234.5] and w.rec["miss_kind"] == "renderer" and w.rec["first_miss_t"] == 1234.5
    assert w.rec["renderer_crash_t"] == 1234.5
    w.poll(), w.poll(), w.poll()
    assert fired == [1234.5]                                                         # one fire between the two paths
    w.cdp_lost(1235.0)
    w.cdp_lost(1236.0)
    assert w.rec["cdp_lost_t"] == 1235.0 and fired == [1234.5]                       # a record, never a trigger
    w2, fired2 = _watch(["gone"] * 5, pid=None)                                      # unarmed pid: a renderer crash
    assert not w2.arm()                                                              # is a positive event and fires
    w2.renderer_crashed(5.0)
    assert fired2 == [5.0] and w2.rec["miss_kind"] == "renderer"


def test_renderer_socket_loss_is_recorded():
    import browser as B

    class WS:
        def settimeout(self, s): pass
        def connect(self, url): pass
        def send(self, m): self.sent = m
        def recv(self): raise ConnectionResetError("chrome went away")
        def close(self): pass
    lost, crashed = [], []
    r = B.RendererCrashWatch("ws://x", crashed.append, lost.append, ws_factory=WS)
    r._run()
    assert crashed == [] and len(lost) == 1 and "ConnectionResetError" in r.error


def test_exit_code_and_accounting(tmp_path):
    import orchestrate as O
    exp = (HERE / "exp.py").read_text()
    assert "EXIT_BROWSER_DEAD = 16" in exp and O.EXIT_BROWSER_DEAD == 16
    i = exp.index('if abort_reason == "browser_dead":')
    assert "sys.exit(EXIT_BROWSER_DEAD)" in exp[i:i + 120]
    assert O.EXIT_BROWSER_DEAD not in O.NOT_A_FAILURE
    assert (O.EXIT_LOGIN_WALL, O.EXIT_PLAYER_FROZEN, O.EXIT_ROOM_ENDED, O.EXIT_PLAYER_DEAD, O.EXIT_STREAM_ENDED,
            O.EXIT_PLAYER_ERROR_HELD) == (3, 5, 6, 7, 12, 13)
    ch = {"handle": "@c"}
    paused, fails, frozen, not_live = {}, {"yt1": 0}, {"yt1": 2}, {}
    O._account("youtube", O.EXIT_BROWSER_DEAD, 0, ch, None, 5.0, paused, fails, frozen, not_live, tmp_path,
               key="yt1", passed=False)
    assert fails == {"yt1": 1} and not_live == {} and frozen == {"yt1": 0}          # counted; channel unmarked
    O._account("twitch", O.EXIT_BROWSER_DEAD, 0, None, {"name": "Just Chatting"}, 6.0, paused, fails, frozen,
               not_live, tmp_path, key="yt1", passed=False)
    assert fails == {"yt1": 2} and not_live == {}                                    # nor the category


def test_meta_and_off_paths():
    exp = (HERE / "exp.py").read_text()
    br = (HERE / "browser.py").read_text()
    assert "if BROWSER_LIVENESS_ABORT:" in exp and "liveness = _liveness_watch(browser)" in exp
    i_stop = exp.index('meta_extra["browser_liveness"] = liveness.stop()')            # stopped before any teardown step
    assert i_stop < exp.index("browser.resumer.stop()") and i_stop < exp.index("            browser.stop()")
    assert "if BROWSER_LIVENESS_ABORT and self.liveness is not None:" in br
    assert L.BROWSER_LIVENESS_ABORT is True and L.POLL_S == 5.0 and L.MISSES_TO_FIRE == 2


def main() -> int:
    import tempfile
    for f in (test_two_misses_fire_once, test_a_recovered_miss_is_no_sign, test_unverified_pid_never_arms,
              test_stopped_never_fires_and_records_no_loss, test_thread_fires_through_on_fire,
              test_renderer_crash_main_page_only, test_renderer_crash_through_the_watch,
              test_renderer_socket_loss_is_recorded, test_meta_and_off_paths):
        f()
    for f in (test_probe, test_exit_code_and_accounting):
        with tempfile.TemporaryDirectory() as t:
            f(Path(t))
    print("test_liveness: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
