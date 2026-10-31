#!/usr/bin/env python3
"""t102 #4 (levi 00402, spec t102_4_liveness_spec_v1): the slot's Chrome is alive, judged by its pid alone.

A dead tw / yt Chrome was never seen before: the slot ran to the schedule's end (yt_100002, about 2155 s). The trigger
is the pid from current_data/chrome{_tag}.pid (start_chrome's $!, Chrome's own pid through in_cgroup.sh's exec), polled
every POLL_S; MISSES_TO_FIRE consecutive misses fire the abort browser_dead. A miss: the pid is gone, a zombie, or its
cmdline no longer carries --remote-debugging-port=<port> (pid reuse). A pid that fails that check at arming never arms
(an unverified pid never fires). A lost CDP socket is recorded only (cdp_lost), never a trigger: under load CDP is the
false-fire risk (levi). A main-page renderer crash (browser.RendererCrashWatch) fires too (levi 00403 (2)). record() is meta.browser_liveness."""
import os
import threading
import time
from pathlib import Path

BROWSER_LIVENESS_ABORT = True    # t102 #4; off = 7809b21c (no watch, no meta key, no exit 16)
# levi 23:45: the renderer path (browser.RendererCrashWatch, a second CDP client on the page) is separable; off = the pid
# path alone (no CDP connection). On: laptop control pair A / B within the S0 spread; the trio dry decides per platform.
# levi 15:32 (8b): off for COUNT_GO. A second CDP client on the page is a debugger attach under
# collector.boundary.player_behaviour_ab, with no OFF arm on the counted host. The pid watch stays. A renderer crash is read
# post hoc from the session's chrome_stderr.log crash lines (the kill-block sources), so a crashed session is never read
# as STALL. meta.browser_liveness then carries no renderer record (no renderer_crash_t / child_crashes).
RENDERER_CRASH_WATCH = False
POLL_S = 5.0
MISSES_TO_FIRE = 2


def _cmdline(pid: int, proc: Path) -> str | None:
    try:
        return (proc / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None


def _state(pid: int, proc: Path) -> str | None:
    """The one-letter state from /proc/<pid>/stat (after the comm's closing paren), None when unreadable."""
    try:
        s = (proc / str(pid) / "stat").read_text()
        return s[s.rindex(")") + 2:].split(" ", 1)[0]
    except (OSError, ValueError, IndexError):
        return None


def probe(pid: int, port: int, proc: Path = Path("/proc"), kill=os.kill) -> str | None:
    """None while the pid is the slot's live Chrome; else the miss kind: gone / zombie / reused."""
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        pass                                     # it exists (another user's: the sudo launch); the cmdline decides
    if _state(pid, proc) == "Z":
        return "zombie"
    cmd = _cmdline(pid, proc)
    if cmd is None:
        return "gone"
    if f"--remote-debugging-port={port}" not in cmd:
        return "reused"
    return None


class LivenessWatch:
    """Polls probe() on a daemon thread; on_fire(t) runs once, at the MISSES_TO_FIRE-th consecutive miss."""

    def __init__(self, pid_fn, port: int, on_fire, probe_fn=None, clock=time.time,
                 poll_s: float = POLL_S, misses_to_fire: int = MISSES_TO_FIRE):
        self.pid_fn, self.port, self.on_fire, self.clock = pid_fn, port, on_fire, clock
        self.probe_fn = probe_fn or (lambda pid: probe(pid, port))
        self.poll_s, self.misses_to_fire = poll_s, misses_to_fire
        self.rec: dict = {"armed": False, "pid": None, "poll_s": poll_s, "misses_to_fire": misses_to_fire,
                          "first_miss_t": None, "fired_t": None, "miss_kind": None, "cdp_lost_t": None,
                          "polls": 0}
        self._misses = 0
        self._fire_lock = threading.Lock()       # the poll thread and the renderer watch's thread fire once between them
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def arm(self) -> bool:
        """The start check: the pid file names a live process that is this slot's Chrome. False = never armed."""
        pid = self.pid_fn()
        self.rec["pid"] = pid
        if not pid:
            self.rec["why"] = "no pid file"
            return False
        kind = self.probe_fn(pid)
        if kind is not None:
            self.rec["why"] = f"pid {kind} at arming"
            return False
        self.rec["armed"] = True
        return True

    def poll(self) -> bool:
        """One poll; True when this poll fired."""
        if not self.rec["armed"] or self.rec["fired_t"] is not None:
            return False
        self.rec["polls"] += 1
        kind = self.probe_fn(self.rec["pid"])
        if kind is None:
            self._misses = 0
            self.rec["first_miss_t"] = self.rec["miss_kind"] = None     # a miss that recovered is no sign
            return False
        now = round(self.clock(), 3)
        self._misses += 1
        if self._misses == 1:
            self.rec["first_miss_t"], self.rec["miss_kind"] = now, kind
        if self._misses < self.misses_to_fire:
            return False
        return self._fire(now)

    def _fire(self, t: float) -> bool:
        with self._fire_lock:
            if self.rec["fired_t"] is not None or self._stop.is_set():     # stopped = teardown: our own kill is no death
                return False
            self.rec["fired_t"] = t
        self.on_fire(t)
        return True

    def renderer_crashed(self, t: float) -> None:
        """levi 00403 (2): the main page's renderer crashed (browser.RendererCrashWatch); the same fire, its own kind."""
        if self.rec["fired_t"] is not None or self._stop.is_set():
            return
        self.rec["renderer_crash_t"] = t
        if self.rec["first_miss_t"] is None:
            self.rec["first_miss_t"], self.rec["miss_kind"] = t, "renderer"
        self._fire(t)

    def cdp_lost(self, t: float) -> None:
        """A record only, never a trigger (levi 00402): when the renderer watch's CDP socket to the page broke."""
        if self.rec["cdp_lost_t"] is None and not self._stop.is_set():
            self.rec["cdp_lost_t"] = t

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                if self.poll():
                    return
            except Exception as e:               # never fatal to the session: recorded, the watch ends
                self.rec["error"] = repr(e)[:200]
                return

    def start(self) -> "LivenessWatch":
        if self.arm():
            self._t = threading.Thread(target=self._run, name="liveness", daemon=True)
            self._t.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=self.poll_s + 1)
        return dict(self.rec)
