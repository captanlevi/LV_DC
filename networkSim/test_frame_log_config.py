#!/usr/bin/env python3
"""t91 (levi 17:4x, boundary 3): browser.frame_log_config, the per-frame rVFC log's switch (LVQOE_FRAME_LOG):
  - unset / "0": None (no extension config, no meta key: the session as without it);
  - "1" on, "alt<N>" OFF / ON blocks of N s from t0; anything else recorded as unparsed, off;
  - levi 18:11: a counted run or a session under a marked record root takes "1" only (recorded record: true); alt<N>
    there, the instruments off or an unparsed value are refused (recorded, off);
  - browser.py writes the extension config only when enabled, and exp.py the meta key only when asked;
  - t93 row 13: trio_slots.TcpStateSampler keeps each socket of `ss -tinH cgroup /<slot>` as printed, ON blocks only,
    capped per sample, and stops on an ss failure, recorded once (never a retry loop).

    python3 test_frame_log_config.py        (exit 0 = pass)
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
import data_root  # noqa: E402
import trio_slots as T  # noqa: E402

SS_OUT = ("ESTAB 0      0      192.168.1.5:50122 142.250.1.1:443\n"
          "\t cubic wscale:7,7 rto:204 rtt:3.1/1.2 cwnd:10 bytes_acked:1200 delivery_rate:9.1Mbps\n"
          "ESTAB 0      0      192.168.1.5:50124 151.101.1.1:443\n"
          "\t cubic rtt:20.5/3 cwnd:44 retrans:0/2\n")


def test_frame_log_config():
    with tempfile.TemporaryDirectory() as t:
        dry = Path(t) / "dry" / "s1"
        rec = Path(t) / "rec" / "_trio" / "s1"
        dry.mkdir(parents=True)
        rec.mkdir(parents=True)
        (Path(t) / "rec" / data_root.RECORD_MARKER).write_text(json.dumps({"uuid": "x"}))
        cfg = lambda raw, d=dry, rec_=False, ins=True: B.frame_log_config(raw, d, rec_, ins)
        assert cfg(None) is None and cfg("") is None and cfg("0") is None
        assert cfg("1") == {"requested": "1", "enabled": True, "block_s": 0, "t0": 0, "clock": "epoch"}
        assert cfg(" alt60 ") == {"requested": "alt60", "enabled": True, "block_s": 60, "t0": 0, "clock": "epoch"}
        for bad in ("alt0", "alt", "on", "2"):
            assert cfg(bad) == {"requested": bad, "enabled": False, "reason": "unparsed (1 or alt<N>)"}, bad
        on_rec = {"requested": "1", "enabled": True, "record": True, "block_s": 0, "t0": 0, "clock": "epoch"}
        assert cfg("1", rec_=True) == on_rec and cfg("1", d=rec) == on_rec      # the counted mode: "1" only
        assert cfg("alt60", rec_=True) == {"requested": "alt60", "enabled": False,
                                           "reason": "of record: alt<N> is dry only"}
        assert cfg("alt60", d=rec) == {"requested": "alt60", "enabled": False,
                                       "reason": "under a record root: alt<N> is dry only"}
        assert cfg("alt60", d=rec, rec_=True)["reason"] == "of record: alt<N> is dry only"
        assert cfg("1", ins=False) == {"requested": "1", "enabled": False, "reason": "instruments off"}
        assert cfg("1", d=rec, rec_=True, ins=False) == {"requested": "1", "enabled": False, "record": True,
                                                         "reason": "instruments off"}
        assert cfg("x", rec_=True)["reason"] == "unparsed (1 or alt<N>)"
        assert cfg("1", d=None)["enabled"]


def test_frame_log_wiring():
    src = (HERE / "browser.py").read_text()
    assert "frame_log_config(os.environ.get(FRAME_LOG_ENV), session_dir," in src
    assert 'if self.frame_log and self.frame_log["enabled"] else {}' in src     # the extension config only when on
    assert "if browser.frame_log:" in (HERE / "exp.py").read_text()             # the meta key only when asked


def test_decode_malformed():
    import frame_log as FL
    ok = {"n": 2, "c": {"pt": [1000, 500], "edt": [None, 250]}}               # row 0 no edt; row 1 pt 1.5 ms
    assert FL.decode(ok) == [{"pt": 1.0}, {"pt": 1.5, "edt": 1.75}]
    for bad in ({"n": 2, "c": {"pt": [None, 500], "edt": [7, None]}},          # a "p" value on a row with no pt
                {"n": 1, "c": {"edt": [7]}},                                   # no pt column at all
                {"n": 2, "c": {"pt": [1000]}}):                                # a column shorter than n
        try:
            FL.decode(bad)
        except FL.FrameLogError:
            continue
        raise AssertionError(bad)


class _Page:
    def __init__(self, flag=True, fail=()):
        self.flag, self.fail, self.calls = flag, fail, []

    def eval(self, js):
        self.calls.append("eval")
        return self.flag

    def call(self, m, params=None):
        self.calls.append(m)
        if m in self.fail:
            raise RuntimeError(m)
        return {"identifier": "3"} if m == "Page.addScriptToEvaluateOnNewDocument" else {}


def _session(frame_log, platform="youtube", join_raises=False):
    bs = B.BrowserSession.__new__(B.BrowserSession)          # no browser: only the wrap's attributes
    bs.frame_log, bs.platform, bs._page, bs._prepared = frame_log, platform, _Page(), True

    def join():
        bs._mse_wrap_install()                               # as _reset_for_session would have, before the join
        if join_raises:
            raise TimeoutError("join timeout")
        bs._mse_wrap_settle_once("join")
    bs._join_nav = join
    return bs


ON = {"requested": "1", "enabled": True, "block_s": 0, "t0": 0, "clock": "epoch"}
FULL = ["Page.enable", "Page.addScriptToEvaluateOnNewDocument", "eval", "Page.removeScriptToEvaluateOnNewDocument",
        "Page.disable"]


def test_mse_wrap_settle():
    pg = _Page()
    r = B.mse_wrap_settle(pg, {"installed": True, "id": "3", "installed_at": 1.0})
    assert r["live"] is True and r["installed_at"] == 1.0 and r["page_disabled"] and "page_disabled_at" in r
    assert pg.calls == FULL[2:]
    assert B.mse_wrap_settle(_Page(None), {"installed": True, "id": "3"})["live"] is False
    r = B.mse_wrap_settle(_Page(fail=("Page.removeScriptToEvaluateOnNewDocument",)), {"id": "3"})
    assert r["page_disabled"] and "remove_error" in r                         # a failed remove never skips the disable
    r = B.mse_wrap_settle(_Page(fail=("Page.disable",)), {"id": "3"})
    assert r["page_disabled"] is False and "disable_error" in r               # recorded, never silent


def test_mse_wrap_unset_no_cdp():
    """levi 00:06 (a): unset / 0 / refused / not yt: the join sends no Page call, registers nothing, settles nothing."""
    for fl, plat in ((None, "youtube"), ({"requested": "0", "enabled": False}, "youtube"),
                     ({"requested": "alt60", "enabled": False, "reason": "x"}, "youtube"), (ON, "twitch"), (ON, "tiktok")):
        bs = _session(None if fl is None else dict(fl), plat)
        bs.start()
        bs._mse_wrap_settle_once("stop")
        assert bs._page.calls == [] and not (bs.frame_log or {}).get("mse_wrap"), (fl, plat)
    src = (HERE / "browser.py").read_text()                 # the only Page domain calls are the wrap's
    for m in ('"Page.enable"', '"Page.disable"', '"Page.removeScriptToEvaluateOnNewDocument"'):
        assert src.count(m) == 1, m
    i = src.index("def _mse_wrap_install(self)")
    assert i < src.index('"Page.enable"') < src.index("def _mse_wrap_settle_once(self")


def test_mse_wrap_disabled_on_every_exit():
    """levi 00:06 (b): Page.disable on the join's normal end, on an exception / timeout (start()'s finally), and at stop()
    when the join never ran; once."""
    bs = _session(dict(ON))
    bs.start()
    bs._mse_wrap_settle_once("stop")
    assert bs._page.calls == FULL and bs.frame_log["mse_wrap"]["settled"] == "join"
    bs = _session(dict(ON), join_raises=True)
    try:
        bs.start()
        raise AssertionError("the join's exception must reach the caller")
    except TimeoutError:
        pass
    w = bs.frame_log["mse_wrap"]
    assert bs._page.calls == FULL and w["settled"] == "join_exit" and w["page_disabled"] is True
    bs = _session(dict(ON))                                     # discover() installed it, start() never ran
    bs._mse_wrap_install()
    bs._mse_wrap_settle_once("stop")
    bs._mse_wrap_settle_once("stop")
    assert bs._page.calls == FULL and bs.frame_log["mse_wrap"]["settled"] == "stop"
    bs = _session(dict(ON))                                     # Page.enable itself failed: still disabled after
    bs._page.fail = ("Page.enable",)
    bs._mse_wrap_install()
    bs._mse_wrap_settle_once("stop")
    w = bs.frame_log["mse_wrap"]
    assert w["installed"] is False and "error" in w and w["page_disabled"] is True and bs._page.calls[-1] == "Page.disable"


def test_tcp_state_sampler():
    assert T.parse_ss_flows(SS_OUT) == [
        "ESTAB 0      0      192.168.1.5:50122 142.250.1.1:443 cubic wscale:7,7 rto:204 rtt:3.1/1.2 cwnd:10 "
        "bytes_acked:1200 delivery_rate:9.1Mbps",
        "ESTAB 0      0      192.168.1.5:50124 151.101.1.1:443 cubic rtt:20.5/3 cwnd:44 retrans:0/2"]
    calls, arm = [], {"on": True}

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout=SS_OUT, stderr="")
    with tempfile.TemporaryDirectory() as t:
        slot = T.slot_of("youtube")
        smp = T.TcpStateSampler(Path(t), slot, lambda: arm["on"], run=run)
        assert smp.cmd == ["ss", "-tinH", "cgroup", "/" + slot.cgroup]
        assert smp.sample()["n"] == 2
        arm["on"] = False
        assert smp.sample() is None and len(calls) == 1                         # OFF block: no ss at all
        rows = [json.loads(x) for x in (Path(t) / "tcp_state.jsonl").read_text().splitlines()]
        assert len(rows) == 1 and rows[0]["flows"][1].endswith("retrans:0/2")
        arm["on"] = True
        smp.run = lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="unknown filter cgroup")
        assert smp.sample() is None and smp._stop.is_set()                      # recorded once, the sampler stops
        st = smp.stop()
        assert (st["samples"], st["off_samples"], st["error"]) == (1, 1, "rc 1: unknown filter cgroup")
        assert st["scope"].startswith("TCP only")
    src = (HERE / "exp.py").read_text()
    assert "trio_slots.TcpStateSampler(session_dir, SLOT," in src and 'browser.frame_log["tcp_state"] = tcp_state.stop()' in src


def main() -> int:
    test_frame_log_config()
    test_frame_log_wiring()
    test_decode_malformed()
    test_mse_wrap_settle()
    test_mse_wrap_unset_no_cdp()
    test_mse_wrap_disabled_on_every_exit()
    test_tcp_state_sampler()
    print("test_frame_log_config: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
