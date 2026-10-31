#!/usr/bin/env python3
"""The page crash record (levi 15:4x / supervisor 15:42; RENDERER_CRASH_WATCH off): MediaListener's existing CDP client
also enables Inspector and records the page's Inspector.targetCrashed / detached / targetReloadedAfterCrash.

  - handle(): each event is a media_events.jsonl row (method "Inspector.<event>", t, params) and fills summary()'s
    page_crash with its first time (a detach also its reason); Media rows and other domains are unchanged; no event ->
    page_crash None;
  - _run(): Inspector.enable goes on the same socket right after Media.enable (one client, no new attach), and a crash
    event read from that socket is written (mutant: the enable dropped);
  - record only: the class never calls a liveness / abort hook (it has none).

    python3 test_page_crash_record.py        (exit 0 = pass)
"""
import json
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


def test_inspector_events_are_recorded_first_time_kept():
    m = B.MediaListener("ws://unused", None)
    assert m.summary()["page_crash"] is None
    assert m.handle({"method": "Inspector.someOther", "params": {}}, 1.0) is None
    r = m.handle({"method": "Inspector.targetCrashed", "params": {}}, 12.5)
    assert r == {"t": 12.5, "method": "Inspector.targetCrashed"}
    m.handle({"method": "Inspector.targetCrashed", "params": {}}, 20.0)                    # a second crash: first kept
    r = m.handle({"method": "Inspector.detached", "params": {"reason": "Render process gone."}}, 13.0)
    assert r["reason"] == "Render process gone." and r["method"] == "Inspector.detached"
    m.handle({"method": "Inspector.targetReloadedAfterCrash", "params": {}}, 14.0)
    assert m.summary()["page_crash"] == {"crashed_t": 12.5, "detached_t": 13.0, "detach_reason": "Render process gone.",
                                         "reloaded_t": 14.0}
    r = m.handle({"method": "Media.playerErrorsRaised", "params": {"playerId": "p", "errors": []}}, 15.0)
    assert r["method"] == "playerErrorsRaised"                                               # Media rows unchanged
    assert [x["method"] for x in m.snapshot()["recent"]] == ["playerErrorsRaised"]           # crash rows not in recent


class _FakeWS:
    sent: list = []
    script: list = []

    def settimeout(self, _):
        pass

    def connect(self, url):
        self.url = url

    def send(self, s):
        _FakeWS.sent.append(json.loads(s))

    def recv(self):
        if _FakeWS.script:
            return json.dumps(_FakeWS.script.pop(0))
        _FakeWS.listener.stop()
        raise _FakeWS.Timeout()

    def close(self):
        pass


def test_run_enables_inspector_on_the_same_socket_and_writes_the_crash():
    class Timeout(Exception):
        pass
    ws_mod = sys.modules["websocket"]
    keep = (getattr(ws_mod, "WebSocket", None), getattr(ws_mod, "WebSocketTimeoutException", None))
    ws_mod.WebSocket, ws_mod.WebSocketTimeoutException, _FakeWS.Timeout = _FakeWS, Timeout, Timeout
    try:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "media_events.jsonl"
            m = B.MediaListener("ws://page", path)
            _FakeWS.sent, _FakeWS.listener = [], m
            _FakeWS.script = [{"method": "Inspector.targetCrashed", "params": {}},
                              {"method": "Inspector.detached", "params": {"reason": "Render process gone."}}]
            m._run()
            assert [x["method"] for x in _FakeWS.sent] == ["Media.enable", "Inspector.enable"], _FakeWS.sent
            rows = [json.loads(x) for x in path.read_text().splitlines()]
            assert [r["method"] for r in rows] == ["Inspector.targetCrashed", "Inspector.detached"]
            assert m.summary()["page_crash"]["crashed_t"] is not None and m.error is None
    finally:
        ws_mod.WebSocket, ws_mod.WebSocketTimeoutException = keep


def test_record_only():
    m = B.MediaListener("ws://unused", None)
    assert not any(hasattr(m, a) for a in ("on_crash", "liveness", "abort"))
    assert "INSPECTOR_EVENTS" in dir(B) and B.INSPECTOR_EVENTS == (
        "Inspector.targetCrashed", "Inspector.detached", "Inspector.targetReloadedAfterCrash")


def main() -> int:
    for f in (test_inspector_events_are_recorded_first_time_kept, test_run_enables_inspector_on_the_same_socket_and_writes_the_crash,
              test_record_only):
        f()
    print("test_page_crash_record: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
