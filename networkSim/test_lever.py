#!/usr/bin/env python3
"""v3 B4 STALL lever (prereg b6bc9246): the plan is drawn from the session's name only (reproducible, no overlap, inside
the session), the rate is RATE_SHARE x the session's lowest rung with the platform fallback when it is unread or the
read fails, the episode clock starts at the first apply, an abort inside an episode (or its grace) is counted, and
_ossilate replaces only the rate inside an episode and tags the net_labels row.

    python3 test_lever.py        (exit 0 = pass)
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lever as L  # noqa: E402

P = {"lambda_per_s": 1 / 120, "d_lo_s": 20, "d_hi_s": 40}


def test_plan_reproducible_non_overlapping():
    a = L.plan("tw_100123", 3600, P["lambda_per_s"], P["d_lo_s"], P["d_hi_s"])
    assert a == L.plan("tw_100123", 3600, P["lambda_per_s"], P["d_lo_s"], P["d_hi_s"])
    assert a != L.plan("tw_100124", 3600, P["lambda_per_s"], P["d_lo_s"], P["d_hi_s"])
    eps = a["episodes"]
    assert eps and [e["id"] for e in eps] == list(range(1, len(eps) + 1))
    for e in eps:
        assert 0 < e["onset_s"] < 3600 and P["d_lo_s"] <= e["d_s"] <= P["d_hi_s"]
    for u, v in zip(eps, eps[1:]):
        assert v["onset_s"] >= u["onset_s"] + u["d_s"]
    for bad in ((0, 20, 40), (0.01, 40, 20), (0.01, 0, 20)):
        try:
            L.plan("x", 3600, *bad)
            raise AssertionError(f"plan{bad} must raise")
        except ValueError:
            pass


def test_rate_source_and_fallback():
    assert L.rate_kbit("youtube", {"kbit": 300, "source": "page_js"}) == (150, "page_js")
    assert L.rate_kbit("twitch", {"kbit": 160, "source": "hls_master"}) == (80, "hls_master")
    for r in (None, {}, {"kbit": 300, "source": "fallback"}, {"kbit": None, "source": "page_js"}):
        assert L.rate_kbit("tiktok", r) == (L.FALLBACK_KBIT["tiktok"], "fallback")
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("cdp gone")
    lv = L.Lever("youtube", "yt_1", 600, P, rung_fn=boom)
    ep = lv.episodes[0]
    assert lv.rate_for(ep, 10.0) == L.FALLBACK_KBIT["youtube"] and lv.rate_for(ep, 12.0) == L.FALLBACK_KBIT["youtube"]
    assert calls == [1]                                            # read once per episode
    assert lv.rates[ep["id"]]["source"] == "fallback" and lv.rates[ep["id"]]["first_applied_t"] == 10.0


def test_clock_and_abort_record():
    lv = L.Lever("twitch", "tw_1", 3600, P, rung_fn=lambda: {"kbit": 200, "source": "hls_master"})
    e1 = lv.episodes[0]
    lv.active(1000.0)
    assert lv.t0 == 1000.0                                          # t0 = the first apply
    assert lv.active(1000.0 + e1["onset_s"] - 0.01) != e1 and lv.active(1000.0 + e1["onset_s"] + 0.01) == e1
    assert lv.active(1000.0 + e1["onset_s"] + e1["d_s"] + 0.01) != e1
    r = lv.record(1000.0 + e1["onset_s"] + e1["d_s"] + L.ABORT_GRACE_S - 1, "player_dead")
    assert r["aborts_in_episode"] == 1 and r["abort_episode"] == e1["id"] and r["abort_reason"] == "player_dead"
    gap_t = next((1000.0 + u["onset_s"] + u["d_s"] + L.ABORT_GRACE_S + 1 for u, v in zip(lv.episodes, lv.episodes[1:])
                  if v["onset_s"] > u["onset_s"] + u["d_s"] + L.ABORT_GRACE_S + 2), None)
    assert gap_t is not None and lv.record(gap_t, "x")["aborts_in_episode"] == 0
    assert lv.record(None)["aborts_in_episode"] == 0


def _ossilate_ns(**stubs):
    """exp._ossilate's own source, exec'd with stubs (exp.py exits at import without the repo's .venv, so no test imports it)."""
    import ast
    import time
    from dataclasses import replace as dc_replace
    from utils.dataModels import NetLabel
    src = (HERE / "exp.py").read_text()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_ossilate")
    ns = {"asyncio": asyncio, "time": time, "dc_replace": dc_replace, "NetLabel": NetLabel, "SYSTEM_TICK": 0.0,
          "SHAPER_OPTS": {}, **stubs}
    exec(compile(ast.Module([fn], []), "exp._ossilate", "exec"), ns)
    return ns


def test_ossilate_overlay():
    from dataclasses import dataclass

    @dataclass
    class NS:
        rate: int
        state: str
        delay_ms: int
        loss_pct: float
        redraw_kbit: object = None
        redraw_delay_ms: object = None
        redraw_loss_pct: object = None
        walk_step: int = 0
        duration: float = 0.0

    class Clock:                                       # each apply reads time twice (apply_start, timestamp)
        def __init__(self, ts):
            self.ts = iter(t for t in ts for _ in (0, 1))

        def time(self):
            return next(self.ts, 1e9)
    lv = L.Lever("twitch", "tw_x", 600, P, rung_fn=lambda: {"kbit": 200, "source": "hls_master"})
    lv.episodes = [{"id": 1, "onset_s": 5.0, "d_s": 20.0}]
    applied, labels = [], []
    ns = _ossilate_ns(SHAPER_GEN=lambda **kw: iter([NS(5000 + i, "s8", 40, 0.1) for i in range(3)]),
                      _apply_net_stat=lambda net_stat: applied.append(net_stat), LEVER=lv)
    ns["time"] = Clock([100.0, 105.0, 140.0])
    assert asyncio.run(ns["_ossilate"](60, labels, asyncio.Event(), {}))
    assert [x.rate for x in applied] == [5000, 100, 5002]          # t 0 / 5 / 40 s: only the episode's apply is replaced
    assert [(x.delay_ms, x.loss_pct, x.state) for x in applied] == [(40, 0.1, "s8")] * 3
    assert [n.lever for n in labels] == [None, 1, None] and [n.speed for n in labels] == [5000, 100, 5002]
    assert lv.record()["rates"]["1"] == {"kbit": 100, "source": "hls_master", "first_applied_t": 105.0,
                                         "rung": {"kbit": 200, "source": "hls_master"}}
    ns = _ossilate_ns(SHAPER_GEN=lambda **kw: iter([NS(7000, "s9", 10, 0.0)]),     # no lever: untouched
                      _apply_net_stat=lambda net_stat: applied.append(net_stat), LEVER=None)
    labels = []
    assert asyncio.run(ns["_ossilate"](60, labels, asyncio.Event(), {})) and labels[0].lever is None and applied[-1].rate == 7000


def test_exp_wiring():
    src = (HERE / "exp.py").read_text()
    assert '"--lever"' in src and "LEVER = _lever.Lever(platform, session_dir.name" in src
    assert "lever=(LEVER.record(browser.abort_at, browser.abort_reason)" in src


def main() -> int:
    test_plan_reproducible_non_overlapping()
    test_rate_source_and_fallback()
    test_clock_and_abort_record()
    test_ossilate_overlay()
    test_exp_wiring()
    print("test_lever: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
