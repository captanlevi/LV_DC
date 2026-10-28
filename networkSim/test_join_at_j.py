#!/usr/bin/env python3
"""rushi 15:20 / levi 15:24-15:25 (kb collector.v3.join_at_fixed_mid_speed): every auto session joins at ONE fixed state J
(8, the band's midpoint tc, no walk, no delay / loss factor), applied before navigation; it settles JOIN_SETTLE_S at J from
join.t_playing; the schedule (W, steering, forced start, the lever's t0) starts after (t_settle_end). The J row carries
phase "join" and is never coverage, never the steering start, never in the label window.

    python3 test_join_at_j.py        (exit 0 = pass)
"""
import ast
import asyncio
import csv
import json
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace as dc_replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from utils.dataModels import NetLabel, NetStat  # noqa: E402
from utils.shaping import SCENARIOS  # noqa: E402

SRC = (HERE / "exp.py").read_text()


def _fns(*names):
    out = [n for n in ast.parse(SRC).body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in names]
    assert len(out) == len(names), names
    return out


def _const(name):
    for n in ast.parse(SRC).body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name:
            return ast.literal_eval(n.value)
    raise AssertionError(name)


@dataclass(frozen=True)
class NS:
    rate: int
    state: str
    duration: float
    delay_ms: int = 10
    loss_pct: float = 0.0
    redraw_kbit: int = 0
    redraw_delay_ms: int = 0
    redraw_loss_pct: float = 0.0
    walk_step: int = 0
    episode: int = 0


class _Lever:
    def __init__(self):
        self.calls = []

    def active(self, t):
        self.calls.append(t)
        return None


def _ns(settle_s=0.3, playing_preset=False, first_preset=False, J="8", hold=False):
    applied, lever = [], _Lever()
    playing, first = threading.Event(), threading.Event()
    if playing_preset:
        playing.set()
    if first_preset:
        first.set()
    ns = {"asyncio": asyncio, "time": time, "dc_replace": dc_replace, "NetLabel": NetLabel, "NetStat": NetStat,
          "SCENARIOS": SCENARIOS, "SYSTEM_TICK": 0.005, "SHAPER_OPTS": {}, "WRAP_SEED": None, "SCHEDULE_WRAP": {},
          "net_episode_generator": None, "family_episode_generator": None, "JOIN_FIRST": first, "JOIN_PLAYING": playing,
          "JOIN": {}, "JOIN_J": J, "JOIN_SETTLE_S": settle_s, "JOIN_CAP_S": 600.0, "LEVER": lever, "DRY_HOLD": hold,
          "SHAPER_GEN": lambda **kw: iter([NS(300, "2", 0.05), NS(900, "4", 0.05)]),
          "_apply_net_stat": lambda net_stat: applied.append((time.time(), net_stat))}
    exec(compile(ast.Module(_fns("_ossilate", "_schedule", "_join_phase", "join_phase_stat", "check_apply", "_hold_j"), []),
                 "exp", "exec"), ns)
    return ns, applied, lever


def _run(ns, labels, stop=None, play_after=None, episodes=None):
    async def go():
        stop_ev = stop or asyncio.Event()
        task = asyncio.create_task(ns["_ossilate"](episode_time_in_seconds=1, net_labels=labels, stop_event=stop_ev,
                                                   transition_dct={}, max_episodes=episodes))
        if play_after is not None:
            while not ns["JOIN_FIRST"].is_set():
                await asyncio.sleep(0.005)
            await asyncio.sleep(play_after)
            ns["JOIN"]["t_playing"] = round(time.time(), 3)           # the join thread: t_playing, then JOIN_PLAYING
            ns["JOIN_PLAYING"].set()
        return await asyncio.wait_for(task, 10)                         # a missing cap fails, never hangs
    return asyncio.run(go())


def test_j_is_state_8_at_its_band_midpoints_and_a_60_s_settle():
    assert _const("JOIN_J") == "8"
    from utils.shaping import JOIN_SETTLE_S
    assert JOIN_SETTLE_S == 60.0 and "SCENARIOS, JOIN_SETTLE_S)" in SRC     # exp uses shaping's
    ns, _, _ = _ns()
    js = ns["join_phase_stat"]("8")
    assert (js.rate, js.delay_ms, js.loss_pct, js.state, js.walk_step) == (3250, 20, 0.075, "8", 0)   # make slow, no walk


def test_j_before_navigation_the_settle_then_the_schedule():
    ns, applied, lever = _ns(settle_s=0.3)
    labels = []
    assert _run(ns, labels, play_after=0.2) is True
    j = ns["JOIN"]
    assert [a[1].state for a in applied] == ["8", "2", "4"]                           # J first, then the schedule
    assert labels[0].phase == "join" and labels[0].state == "8" and labels[0].speed == 3250
    assert [l.phase for l in labels[1:]] == [None, None]
    assert (j["J"], j["first_state"], j["first_rate_kbit"]) == ("8", "8", 3250)      # browser's waits scale to J
    assert j["t_J_apply"] == j["t_first_apply"] == round(labels[0].apply_start, 3)   # before navigation (JOIN_FIRST)
    first_sched = applied[1][0]
    assert first_sched >= j["t_playing"] + 0.3 - 0.01 and j["t_settle_end"] <= first_sched + 0.01
    assert abs(first_sched - (j["t_playing"] + 0.3)) < 0.1                           # levi's PASS item 1: +/- 2 s
    assert lever.calls and j["t_settle_end"] - 0.01 <= lever.calls[0] <= first_sched   # the lever's t0 (its first active()):
    assert lever.calls[0] - j["t_J_apply"] >= 0.3 + 0.2 - 0.02                          # the first schedule apply, never J's
    assert len(lever.calls) == 2                                                      # never consulted during J


def test_nothing_played_by_the_cap_returns_without_the_schedule():
    ns, applied, _ = _ns()
    labels = []
    t0 = time.time()
    assert _run(ns, labels, episodes=1) is False                                     # cap = 1 episode x 1 s
    assert 0.9 < time.time() - t0 < 3.0 and [a[1].state for a in applied] == ["8"]
    assert "t_settle_end" not in ns["JOIN"]                                           # after_schedule: join_timeout (D2)


def test_a_stop_during_the_settle_never_starts_the_schedule():
    ns, applied, _ = _ns(settle_s=5.0)
    stop = asyncio.Event()

    async def go():
        task = asyncio.create_task(ns["_ossilate"](episode_time_in_seconds=1, net_labels=[], stop_event=stop,
                                                   transition_dct={}))
        while not ns["JOIN_FIRST"].is_set():
            await asyncio.sleep(0.005)
        ns["JOIN"]["t_playing"] = time.time()
        ns["JOIN_PLAYING"].set()
        await asyncio.sleep(0.2)
        stop.set()
        return await task
    assert asyncio.run(go()) is False and [a[1].state for a in applied] == ["8"]


def test_manual_mode_an_interactive_restart_and_no_j_skip_the_phase():
    for kw in ({"playing_preset": True}, {"first_preset": True, "playing_preset": True}, {"J": None}):
        ns, applied, _ = _ns(**kw)
        labels = []
        if kw.get("J", "8") is None:
            assert _run(ns, labels, play_after=0.0) is True
        else:
            assert _run(ns, labels) is True
        assert [a[1].state for a in applied] == ["2", "4"] and all(l.phase is None for l in labels), kw


def test_join_record_at_j_plays_the_whole_schedule():
    ns: dict = {}
    exec(compile(ast.Module(_fns("join_record"), []), "exp", "exec"), ns)
    r = ns["join_record"](1000.0, 1000.5, 1030.0, 300.0, settle_s=60.0)
    assert r == {"join_s": 29.5, "session_end_planned": 1390.0, "schedule_played_s": 300.0}
    assert ns["join_record"](1000.0, 1000.5, None, 300.0, settle_s=60.0) == {}       # never played: no planned end
    assert ns["join_record"](1000.0, 1000.0, 1300.0, 1800.0)["schedule_played_s"] == 1500.0   # pre-J: unchanged


def test_after_schedule_records_the_settle_end():
    ns = {"JOIN": {"outcome": "playing", "t_settle_end": 1090.0}, "time": time, "JOIN_STOP_WAIT_S": 0.3}
    exec(compile(ast.Module(_fns("join_meta", "join_unfinished_reason", "after_schedule"), []), "exp", "exec"), ns)
    done = threading.Thread(target=lambda: None)
    done.start()
    wrote = []
    assert ns["after_schedule"](done, None, None, write=wrote.append) is None
    assert wrote and wrote[-1]["t_settle_end"] == 1090.0


def _rows():
    return [NetLabel(timestamp=1000.0, speed=3250, state="8", phase="join"),
            NetLabel(timestamp=1090.0, speed=300, state="2"), NetLabel(timestamp=1120.0, speed=900, state="4")]


def test_the_steering_start_and_the_balancer_never_read_the_j_row():
    import session_meta
    import balance
    from dataclasses import asdict
    assert session_meta.steering_start_applied(_rows()) == "2"
    assert session_meta.steering_start_applied([asdict(r) for r in _rows()]) == "2"
    assert session_meta.steering_start_applied([NetLabel(timestamp=1.0, speed=1, state="5")]) == "5"   # pre-J rows
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "net_labels.txt"
        p.write_text("".join(json.dumps(asdict(r)) + "\n" for r in _rows()))
        got = balance.load_run(p)
        assert [r["state"] for r in got] == ["2", "4"]
        assert "8" not in balance.time_weighted_dist(got)


def test_the_label_window_and_coverage_start_at_the_settle_end():
    import check_session
    import coverage_report
    from dataclasses import asdict
    net = [asdict(r) for r in _rows()]
    meta = {"ended_at": 1400.0, "join": {"t_playing": 1030.0, "t_settle_end": 1090.0}}
    assert check_session.session_window(meta, net) == (1090.0, 1400.0)
    assert check_session.session_window({"ended_at": 1400.0, "join": {"t_playing": 1030.0}}, net) == (1030.0, 1400.0)
    stopped = {"ended_at": 1060.0, "join": {"J": "8", "t_J_apply": 1000.0, "t_playing": 1030.0}}   # stopped in the settle
    assert check_session.session_window(stopped, net) is None                          # levi 16:03: no schedule seconds
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        with open(sd / "labels.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["ts", "type", "is_ad", "video_w", "video_h", "buffered_ahead_s"])
            w.writeheader()
            for t in range(1031, 1150):                                               # 59 settle ticks, 60 schedule
                w.writerow({"ts": t * 1000, "type": "res_meta", "is_ad": "false", "video_w": 1920, "video_h": 1080,
                            "buffered_ahead_s": 10})
        (sd / "meta.json").write_text(json.dumps(meta))
        assert sum(coverage_report.session_classes(sd)["minutes"].values()) == 1.0
        (sd / "meta.json").write_text(json.dumps({"join": {"t_playing": 1030.0}}))      # pre-J: every tick
        assert sum(coverage_report.session_classes(sd)["minutes"].values()) == round(119 / 60, 3)
        (sd / "meta.json").write_text(json.dumps(stopped))                              # J, stopped in the settle: 0
        assert coverage_report.session_classes(sd)["minutes"] == {}


def test_dry_hold_keeps_j_the_whole_session_with_one_apply():
    """levi / shagun 03:19 (LVDC_DRY_HOLD_J): after the settle the schedule never plays; J (its join tc) stays on the
    link for the schedule's length: one tc apply, no walk step; one hold row (phase None, apply "hold", J's tc)."""
    ns, applied, lever = _ns(settle_s=0.1, hold=True)
    labels = []
    t0 = time.time()
    assert _run(ns, labels, play_after=0.05, episodes=1) is True                     # hold = 1 episode x 1 s
    assert [a[1].state for a in applied] == ["8"] and len(applied) == 1               # the join's apply only
    assert [(l.phase, l.apply, l.state, l.speed, l.delay_ms, l.loss_pct) for l in labels] == [
        ("join", "slow", "8", 3250, 20, 0.075), (None, "hold", "8", 3250, 20, 0.075)]
    assert all(not l.walk_step for l in labels) and not lever.calls                   # no walk, no lever
    assert labels[1].timestamp >= ns["JOIN"]["t_settle_end"] - 0.01
    assert 1.0 <= time.time() - t0 < 3.0                                               # the schedule's length still ends it
    ns, applied, _ = _ns(settle_s=0.05, hold=True)                                    # a stop during the hold: False
    stop = asyncio.Event()

    async def go():
        task = asyncio.create_task(ns["_ossilate"](episode_time_in_seconds=60, net_labels=[], stop_event=stop,
                                                   transition_dct={}, max_episodes=1))
        while not ns["JOIN_FIRST"].is_set():
            await asyncio.sleep(0.005)
        ns["JOIN"]["t_playing"] = round(time.time(), 3)
        ns["JOIN_PLAYING"].set()
        await asyncio.sleep(0.2)
        stop.set()
        return await asyncio.wait_for(task, 5)
    assert asyncio.run(go()) is False and len(applied) == 1


def test_dry_hold_refusals_and_wiring():
    """LVDC_DRY_HOLD_J: only "1", dry pilots only (a counted run / record-marker root refuses), --auto only, never with
    another schedule; refused right after C8 / the frame-log refusal, before any session dir and the browser."""
    import types
    ns = {"DRY_HOLD_ENV": _const("DRY_HOLD_ENV")}
    exec(compile(ast.Module(_fns("dry_hold_refusal"), []), "exp", "exec"), ns)
    ref = ns["dry_hold_refusal"]
    A = lambda **kw: types.SimpleNamespace(**{"auto": True, "hold_state": None, "lever": False, "replay_schedule": None, **kw})
    E = "LVDC_DRY_HOLD_J"
    assert ns["DRY_HOLD_ENV"] == E
    assert ref({}, False, A()) is None and ref({E: ""}, True, A()) is None              # unset: unchanged
    assert ref({E: "1"}, False, A()) is None                                           # honoured on a dry pilot
    assert "dry pilots only" in ref({E: "1"}, True, A())                               # counted / record root
    for bad in ("0", "yes", "J"):
        assert "only" in ref({E: bad}, False, A()), bad
    assert "--auto" in ref({E: "1"}, False, A(auto=False))
    for kw in ({"hold_state": "3"}, {"lever": True}, {"replay_schedule": "s"}):
        assert "excludes" in ref({E: "1"}, False, A(**kw)), kw
    assert "excludes" in ref({E: "1", "LVDC_STEER_TABLE": "/t"}, False, A())
    i = SRC.index("_dhr = dry_hold_refusal(os.environ,")
    assert SRC.index("FRAME_LOG REFUSED") < i < SRC.index("session_dir.mkdir(parents=True, exist_ok=True)") \
        < SRC.index("browser = BrowserSession(")
    assert "sys.exit(EXIT_DRY_HOLD_REFUSED)" in SRC and "EXIT_DRY_HOLD_REFUSED = 15" in SRC
    assert "not args.replay_schedule and not DRY_HOLD" in SRC                         # no W rotation on a hold
    assert 'session_meta.merge(session_dir, "shaper", hold={' in SRC


def test_join_meta_names_the_j_rule():
    ns: dict = {}
    exec(compile(ast.Module(_fns("join_meta"), []), "exp", "exec"), ns)
    assert ns["join_meta"]({"J": "8"})["rule"].startswith("J (rushi 15:20, levi 15:25): the fixed join state J")
    assert ns["join_meta"]({})["rule"].startswith("B9 (levi 23:21-23:22, 23:49)")


def test_wiring():
    main = SRC[SRC.index('if __name__ == "__main__":'):]
    join = main[main.index("    def _join(browser) -> None:"):main.index("    browser = None\n")]
    assert 'settle_s=JOIN.get("settle_s")))' in join
    osc = ast.get_source_segment(SRC, _fns("_ossilate")[0])
    assert osc.index("await _join_phase(") < osc.index("for i_change, net_stat in enumerate(_schedule(")
    assert "from browser import BrowserSession, FROZEN_HOLD_RULE, JOIN_CAP_S" in SRC


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_join_at_j: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
