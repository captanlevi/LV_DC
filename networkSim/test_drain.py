#!/usr/bin/env python3
"""v3 A12 (levi 21:06 / 23:19; peter's Chrome test 23:20): CaptureDrain, inside BrowserSession.stop() after the label export
and before Page.close, fails every new request on the page and on each auto-attached worker (Fetch.enable + failRequest,
counted) while in-flight responses land, waits for the downstream bytes to be flat DRAIN_FLAT_S (reason quiet) or
DRAIN_MAX_S (cap), keeps interception on until close() after Page.close, never raises, and exp.py records meta drain
after stop().

    python3 test_drain.py        (exit 0 = pass)
"""
import inspect
import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    ws_mod = types.ModuleType("websocket")
    ws_mod.WebSocketTimeoutException = type("WebSocketTimeoutException", (Exception,), {})
    ws_mod.WebSocket = object
    sys.modules["websocket"] = ws_mod
import browser as B  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeWS:
    def __init__(self, clock, events=(), fail_connect=False):
        self.clock, self.events, self.fail_connect = clock, list(events), fail_connect
        self.sent, self.closed = [], False

    def settimeout(self, t):
        self.timeout = t

    def connect(self, url):
        if self.fail_connect:
            raise ConnectionRefusedError("no cdp")

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        self.clock.t += 0.05
        if self.events:
            return json.dumps(self.events.pop(0))
        raise B.websocket.WebSocketTimeoutException()

    def close(self):
        self.closed = True


def _bytes(clock, until_s, rate=1000):
    t0 = clock.t
    return lambda: int(min(clock.t - t0, until_s) * rate)


def test_quiet_and_blocking():
    c = Clock()
    ev = [{"method": "Target.attachedToTarget", "params": {"sessionId": "W1", "targetInfo": {"type": "worker"}}},
          {"method": "Fetch.requestPaused", "params": {"requestId": "p1"}},
          {"method": "Fetch.requestPaused", "params": {"requestId": "w1"}, "sessionId": "W1"},
          {"method": "Target.attachedToTarget", "params": {"sessionId": "F1", "targetInfo": {"type": "iframe"}}},
          {"method": "Fetch.requestPaused", "params": {"requestId": "f1"}, "sessionId": "F1"},
          {"method": "Target.attachedToTarget", "params": {"sessionId": "X1", "targetInfo": {"type": "other"}}},
          {"method": "Target.attachedToTarget", "params": {"sessionId": "F2", "targetInfo": {"type": "iframe"}},
           "sessionId": "F1"}]                                                  # a nested OOPIF, attached via F1
    ws = FakeWS(c, ev)
    d = B.CaptureDrain("ws://x", _bytes(c, 1.0), "lvi_yt1", ws_factory=lambda: ws, clock=c, sleep=c.sleep)
    r = d.run()
    assert r["blocked"] is True and r["reason"] == "quiet" and r["error"] is None, r
    assert 1.0 + B.DRAIN_FLAT_S - 0.15 <= r["wait_s"] <= 1.0 + B.DRAIN_FLAT_S + 0.3, r
    assert r["bytes_during"] == 1000 and r["requests_failed"] == {"page": 1, "worker": 1, "frame": 1}
    assert r["workers_attached"] == 1 and r["frames_attached"] == 2             # brad 16:39: OOPIF targets blocked too
    m = [(x["method"], x.get("sessionId")) for x in ws.sent]
    assert m[:2] == [("Target.setAutoAttach", None), ("Fetch.enable", None)]
    assert ("Fetch.enable", "W1") in m and ("Fetch.failRequest", None) in m and ("Fetch.failRequest", "W1") in m
    assert ("Fetch.enable", "F1") in m and ("Fetch.failRequest", "F1") in m and ("Fetch.enable", "X1") not in m
    assert ("Target.setAutoAttach", "F1") in m and ("Fetch.enable", "F2") in m       # levi 16:48: recursive
    assert ("Target.setAutoAttach", "W1") not in m
    assert all(x["params"].get("errorReason") == "Aborted" for x in ws.sent if x["method"] == "Fetch.failRequest")
    assert not ws.closed
    d.close()
    assert ws.closed


def test_cap_no_counter_and_failure():
    c = Clock()
    r = B.CaptureDrain("ws://x", _bytes(c, 999.0), "all_interfaces", ws_factory=lambda: FakeWS(c), clock=c,
                       sleep=c.sleep).run()
    assert r["reason"] == "cap" and B.DRAIN_MAX_S <= r["wait_s"] <= B.DRAIN_MAX_S + 0.3, r
    c = Clock()
    r = B.CaptureDrain("ws://x", lambda: None, "x", ws_factory=lambda: FakeWS(c), clock=c, sleep=c.sleep).run()
    assert r["reason"] == "no_counter" and r["wait_s"] < 0.2
    c = Clock()
    r = B.CaptureDrain("ws://x", _bytes(c, 0.5), "x", ws_factory=lambda: FakeWS(c, fail_connect=True), clock=c,
                       sleep=c.sleep).run()
    assert r["blocked"] is False and "no cdp" in r["error"] and r["reason"] == "quiet", r     # still waits, never raises


def test_order_in_stop_and_meta():
    src = inspect.getsource(B.BrowserSession.stop)
    i_exp, i_run = src.index("_export_via_service_worker(self._session_dir)"), src.index("self.drain = drain.run()")
    i_close, i_off = src.index('self._page.call("Page.close")'), src.index("drain.close()")
    assert i_exp < i_run < i_close < i_off
    exp = (HERE / "exp.py").read_text()
    assert exp.index("browser.stop()") < exp.index('meta_extra["drain"] = browser.drain')


def main() -> int:
    test_quiet_and_blocking()
    test_cap_no_counter_and_failure()
    test_order_in_stop_and_meta()
    print("test_drain: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
