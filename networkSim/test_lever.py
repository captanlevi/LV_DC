#!/usr/bin/env python3
"""v3 B4 STALL lever (prereg b6bc9246): the plan is drawn from the session's name only (reproducible, no overlap, inside
the session), the rate is RATE_SHARE x the session's lowest rung read once at start (fallback when unread), fixed for
every episode, with no live read in the shaper path (levi 19:49), the episode clock starts at the first apply, an abort inside an episode (or its grace) is counted, and
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
    for bad in ((-0.01, 20, 40), (0.01, 40, 20), (0.01, 0, 20), (0, 40, 20)):
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
    lv = L.Lever("youtube", "yt_1", 600, P)
    ep = lv.episodes[0]
    assert lv.rate_for(ep, 10.0) == L.FALLBACK_KBIT["youtube"] and lv.rate_for(ep, 12.0) == L.FALLBACK_KBIT["youtube"]
    assert lv.rate["source"] == "fallback" and "no offer read by the first onset" in lv.rate["note"]
    assert lv.first_applied == {ep["id"]: 10.0}
    lv = L.Lever("tiktok", "tt_1", 600, P)
    assert lv.fix_rate({"kbit": 2400, "source": "room_js"}, 5.0)["kbit"] == 1200
    assert lv.fix_rate({"kbit": 100, "source": "room_js"}, 6.0) is None and lv.rate["kbit"] == 1200   # first fix wins
    assert all(lv.rate_for(e, 100.0 + i) == 1200 for i, e in enumerate(lv.episodes))   # every episode, one rate


def test_no_live_read_in_the_lever():
    """levi 19:49: the shaper path makes no live player read. lever.py imports nothing that reaches the player; the rate
    is fixed from a value (never a callable) once; _ossilate's body names no browser / rung read; exp.py fixes the rate
    before the shaper starts."""
    import ast
    import inspect
    tree = ast.parse((HERE / "lever.py").read_text())
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
           {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= {"__future__", "random", "threading"}, mods
    code = (HERE / "lever.py").read_text().split('"""', 2)[2]
    for word in ("browser", "_page", "eval(", "CDP", "rung_fn", "labels.csv", "net_labels"):
        assert word not in code, word
    assert list(inspect.signature(L.Lever).parameters) == ["platform", "session_name", "session_s", "params"]
    src = (HERE / "exp.py").read_text()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_ossilate")
    body = ast.get_source_segment(src, fn)
    for word in ("browser", "_current_rung", "eval(", "fix_rate"):
        assert word not in body, word
    i_fix = src.index("if not _lever_read():")
    assert i_fix < src.index("abort_reason = asyncio.run(experiment(") and src.count("_current_rung") == 1
    assert "_lever_deadline = time.time() + LEVER.first_onset_s - 2" in src     # the retries end before the 1st onset
    assert "LEVER.fix_rate(r, time.time(), deadline=_lever_deadline)" in src     # and a late read is refused


def test_no_episode_before_the_fix():
    """levi 19:57 / 19:58: t0 = the shaper's first apply; the first onset is >= FIRST_ONSET_MIN_S after it; the start
    reader runs before t0 and stops 2 s before the first onset, so a fix it makes always precedes every episode."""
    assert L.FIRST_ONSET_MIN_S == 60.0
    for i in range(300):
        p = L.plan(f"s_{i}", 3600, 1 / 30, 5, 20)
        assert not p["episodes"] or p["episodes"][0]["onset_s"] >= L.FIRST_ONSET_MIN_S
    lv = L.Lever("youtube", "yt_fix", 3600, P)
    t_start = 500.0                                   # the reader's start (before asyncio.run, so <= t0)
    lv.fix_rate({"kbit": 300, "source": "page_js"}, t_start + lv.first_onset_s - 2.5)   # the latest a retry can fix
    t0 = t_start + 0.3
    assert lv.active(t0) is None
    e1 = lv.episodes[0]
    assert lv.rate["at"] < t0 + e1["onset_s"]
    lv.rate_for(e1, t0 + e1["onset_s"])
    late = L.Lever("youtube", "yt_fix", 3600, P)                  # brad 20:06: a read returning past the deadline
    dl = t_start + late.first_onset_s - 2
    assert late.fix_rate({"kbit": 300, "source": "page_js"}, dl + 20.0, deadline=dl) is None and late.rate is None
    assert late.late_reads == [20.0]
    late.active(t0)
    late.rate_for(late.episodes[0], t0 + late.first_onset_s)
    assert late.record()["rung_source"] == "fallback" and late.record()["fix_margin_s"] == 0.0
    r = lv.record()
    assert r["fix_t"] == lv.rate["at"] and r["rung_source"] == "page_js" and r["t0"] == t0
    assert r["fix_margin_s"] > 0
    assert r["episodes_never_applied"] == [e["id"] for e in lv.episodes[1:]]


def test_shaper_items_are_walk_steps():
    """brad / levi 19:58: an episode applies only when a shaper item starts inside it. Items are the walk's sub-steps:
    WALK_STEP_S 2 s, the last of a sub-step in [WALK_LAST_MIN_S, WALK_STEP_S + WALK_LAST_MIN_S), so < 3 s (not <= 2 s);
    exp.py always runs the walk (walk_rng set from the session's name), without which an item is a whole sub-step."""
    import random as _r
    from utils import netStat as NS
    rng, worst = _r.Random("items"), 0.0
    for i in range(2000):
        dur = _r.Random(i).uniform(0.5, 90.0)
        rows = NS.substep_changes("5", 1200, dur, 40, 0.2, rng)
        worst = max(worst, max(r.duration for r in rows))
    assert worst < NS.WALK_STEP_S + NS.WALK_LAST_MIN_S and NS.WALK_STEP_S + NS.WALK_LAST_MIN_S == 3.0, worst
    src = (HERE / "exp.py").read_text()
    assert "SHAPER_OPTS.update(walk_rng=WALK_RNG" in src


def test_clock_and_abort_record():
    lv = L.Lever("twitch", "tw_1", 3600, P)
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


def test_lambda_zero_disables_the_lever():
    """levi 17:21: lambda 0 on a platform (the counted file's tt) draws no plan, applies nothing and is recorded."""
    assert L.plan("ti_1", 3600, 0, 14.4, 44.4) == {"episodes": [], "skipped_overlapping": 0}
    lv = L.Lever("tiktok", "ti_1", 3600, {**P, "lambda_per_s": 0})
    assert lv.episodes == [] and lv.first_onset_s is None and lv.disabled
    assert all(lv.active(1000.0 + t) is None for t in range(0, 3600, 7))
    r = lv.record(None)
    assert r["lever_disabled"] is True and r["episodes"] == [] and r["rate"] is None and r["first_applied"] == {}
    assert r["fix_margin_s"] is None and r["episodes_never_applied"] == [] and r["params"]["lambda_per_s"] == 0
    assert L.Lever("twitch", "tw_1", 3600, P).record(None)["lever_disabled"] is False
    src = (HERE / "exp.py").read_text()                             # the rung read and its retries need episodes
    assert "if LEVER is not None and browser is not None and LEVER.episodes:" in src


def _ossilate_ns(**stubs):
    """exp._ossilate's own source, exec'd with stubs (exp.py exits at import without the repo's .venv, so no test imports it)."""
    import ast
    import time
    from dataclasses import replace as dc_replace
    from utils.dataModels import NetLabel
    src = (HERE / "exp.py").read_text()
    fns = [n for n in ast.parse(src).body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
           and n.name in ("_ossilate", "_schedule")]                # W: _ossilate plays _schedule's changes
    ns = {"asyncio": asyncio, "time": time, "dc_replace": dc_replace, "NetLabel": NetLabel, "SYSTEM_TICK": 0.0,
          "SHAPER_OPTS": {}, "WRAP_SEED": None, "SCHEDULE_WRAP": {}, "net_episode_generator": None,
          "family_episode_generator": None, **stubs}
    exec(compile(ast.Module(fns, []), "exp._ossilate", "exec"), ns)
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
    lv = L.Lever("twitch", "tw_x", 600, P)
    lv.fix_rate({"kbit": 200, "source": "hls_master"}, 99.0)
    lv.episodes = [{"id": 1, "onset_s": 5.0, "d_s": 20.0}]
    applied, labels = [], []
    ns = _ossilate_ns(SHAPER_GEN=lambda **kw: iter([NS(5000 + i, "s8", 40, 0.1) for i in range(3)]),
                      _apply_net_stat=lambda net_stat: applied.append(net_stat), LEVER=lv)
    ns["time"] = Clock([100.0, 105.0, 140.0])
    assert asyncio.run(ns["_ossilate"](60, labels, asyncio.Event(), {}))
    assert [x.rate for x in applied] == [5000, 100, 5002]          # t 0 / 5 / 40 s: only the episode's apply is replaced
    assert [(x.delay_ms, x.loss_pct, x.state) for x in applied] == [(40, 0.1, "s8")] * 3
    assert [n.lever for n in labels] == [None, 1, None] and [n.speed for n in labels] == [5000, 100, 5002]
    r = lv.record()
    assert r["rate"]["kbit"] == 100 and r["rate"]["at"] == 99.0 and r["first_applied"] == {"1": 105.0}
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
    test_no_live_read_in_the_lever()
    test_no_episode_before_the_fix()
    test_shaper_items_are_walk_steps()
    test_clock_and_abort_record()
    test_lambda_zero_disables_the_lever()
    test_ossilate_overlay()
    test_exp_wiring()
    print("test_lever: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
