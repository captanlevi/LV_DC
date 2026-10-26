#!/usr/bin/env python3
"""Restart prereg 2026-10-21 §2 (levi; the walk in linear rate): utils/netStat's walk, the per-session delay / loss
factor and the realised values in net_labels. Covered:
  - the walk: reflected into the state's band, a step every 2 s while >= 1 s of the sub-step remains, sigma = the
    calibrated share of the band (the reference sub-step's span is half the band), the band mean kept;
  - determinism: seeded per session name; the walk never draws from the global random, so the state sequence, the
    re-draws and the sub-step durations are the ones without it (the occupancy fixture is untouched);
  - the factor: log-U(0.5, 2), one per session, on the re-drawn delay and loss; recorded;
  - the record: net_labels' realised and re-draw fields, exp.py's wiring, meta.json shaper, the schedule clock;
  - readers: check_session's sub-steps, the dry run's plans; the committed simulation's sha.
Synthetic; no browser, no root.

    python3 test_shaping_walk.py        (exit 0 = pass)
"""
import hashlib
import importlib.util
import inspect
import math
import random
import sys
from dataclasses import fields
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as CS  # noqa: E402
import session_meta  # noqa: E402
import walk_simulation as WS  # noqa: E402
from utils import netStat as NS, shaping as SH  # noqa: E402
from utils.dataModels import NetLabel  # noqa: E402


def _substeps(rows):
    """[[rows of one sub-step]] by walk_step 0."""
    out = []
    for r in rows:
        if r.walk_step == 0:
            out.append([])
        out[-1].append(r)
    return out


def test_reflect_and_the_band():
    assert NS.reflect(5.0, 0.0, 10.0) == 5.0 and NS.reflect(-3.0, 0.0, 10.0) == 3.0 and NS.reflect(13.0, 0.0, 10.0) == 7.0
    assert NS.reflect(27.0, 0.0, 10.0) == 7.0 and NS.reflect(-21.0, 0.0, 10.0) == 1.0     # several folds
    rng = random.Random("band")
    for st, sc in SH.SCENARIOS.items():
        x = float(sc.rate_min)                     # from the wall: the first steps reflect
        for _ in range(3000):
            x = NS.walk_step(x, st, rng)
            assert sc.rate_min <= x <= sc.rate_max, (st, x)
        rows = NS.substep_changes(st, sc.rate_max, 400.0, 10, 0.1, random.Random(st))
        assert all(sc.rate_min <= r.rate <= sc.rate_max and isinstance(r.rate, int) for r in rows), st
        assert len({r.rate for r in rows}) > 50, st                                   # it moves


def test_a_step_every_two_seconds():
    rng = random.Random(0)
    for dur, starts in ((21.0, [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20]), (22.9, list(range(0, 21, 2))),
                        (23.0, list(range(0, 23, 2))), (3.0, [0, 2]), (2.9, [0]), (0.5, [0]), (12.0, list(range(0, 12, 2)))):
        rows = NS.substep_changes("5", 1200, dur, 40, 0.2, rng)
        t = [sum(r.duration for r in rows[:k]) for k in range(len(rows))]
        assert [round(x, 9) for x in t] == starts, (dur, t)
        assert [r.walk_step for r in rows] == list(range(len(rows)))
        assert abs(sum(r.duration for r in rows) - dur) < 1e-9
        assert all(r.duration >= min(dur, NS.WALK_LAST_MIN_S) - 1e-9 for r in rows)  # the last one >= 1 s
        assert rows[0].rate == 1200 and all(r.redraw_kbit == 1200 and r.state == "5" for r in rows)
    one = NS.substep_changes("5", 1200, 21.0, 40, 0.2)                                 # no walk generator: one change
    assert len(one) == 1 and one[0].rate == 1200 and one[0].duration == 21.0 and one[0].walk_step == 0


def test_sigma_spans_half_the_band():
    assert NS.WALK_STEP_S == 2.0 and NS.WALK_REF_STEPS == 10 and NS.WALK_SPAN_SHARE == 0.5
    assert NS.walk_sigma("10") == NS.WALK_SIGMA_PER_BAND * 3000 and NS.walk_sigma("1") == NS.WALK_SIGMA_PER_BAND * 130
    # the walk as run over the reference sub-step (10 steps from the uniform re-draw): half the band, every state
    assert abs(WS.reference_span(NS.WALK_SIGMA_PER_BAND, n=3000) - 0.5) < 0.012
    assert WS.reference_span(0.1248, n=3000) < 0.44            # Spitzer's unreflected sigma: the walls compress it
    # a 21 s sub-step from the generator's own rows: the same span
    rng, spans = random.Random("span"), []
    for i in range(3000):
        st = str(1 + i % 10)
        sc = SH.SCENARIOS[st]
        rows = NS.substep_changes(st, rng.randint(sc.rate_min, sc.rate_max), 21.0, 0, 0.0, rng)
        assert len(rows) == 11
        spans.append((max(r.rate for r in rows) - min(r.rate for r in rows)) / (sc.rate_max - sc.rate_min))
    assert abs(sum(spans) / len(spans) - 0.5) < 0.02, sum(spans) / len(spans)


def test_the_band_mean_is_kept():
    rng = random.Random("mean")
    for st in ("1", "6", "10"):
        sc = SH.SCENARIOS[st]
        real = redraw = 0.0
        for _ in range(4000):
            r0 = rng.randint(sc.rate_min, sc.rate_max)
            for r in NS.substep_changes(st, r0, 21.0, 0, 0.0, rng):
                real += r.rate * r.duration
                redraw += r.redraw_kbit * r.duration
        assert abs(real / redraw - 1) < 0.01, (st, real / redraw)         # uniform stationary law = the re-draw's


def _gen(walk, factor, family=True, seed=7):
    t, cap, _ = SH.PLATFORM_MATRICES["twitch"]["TW-M@fast" if family else "TW-M"]
    random.seed(seed)
    if family:
        sc = SH.schedule_of("TW-M@fast", cap)
        g = NS.family_episode_generator(sc["episode_s"], t, tuple(sc["redraw_s"]), sc["low_cap_s"], sc["self_loops"],
                                        max_episodes=40, initial_state_fn=SH.initial_state_stationary,
                                        walk_rng=walk, dl_factor=factor)
    else:
        g = NS.net_episode_generator(60, t, max_episodes=12, max_consecutive_low=cap,
                                     initial_state_fn=SH.initial_state_stationary, walk_rng=walk, dl_factor=factor)
    rows = list(g)
    return rows, random.getstate()


def test_the_walk_leaves_the_schedule_alone():
    for family in (True, False):
        plain, s_plain = _gen(None, 1.0, family)
        walked, s_walk = _gen(random.Random("w"), 1.37, family)
        assert s_plain == s_walk                                  # the walk and the factor never touch the global random
        assert len(walked) > len(plain) and all(r.walk_step == 0 for r in plain)
        subs = _substeps(walked)
        assert len(subs) == len(plain)
        for p, sub in zip(plain, subs):                        # same state, re-draw and duration per sub-step
            assert {r.state for r in sub} == {p.state} and sub[0].redraw_kbit == p.rate == sub[0].rate
            assert abs(sum(r.duration for r in sub) - p.duration) < 1e-9
            assert all(r.redraw_delay_ms == p.delay_ms and r.redraw_loss_pct == p.loss_pct for r in sub)
            assert all(r.delay_ms == int(round(p.delay_ms * 1.37)) and r.loss_pct == round(p.loss_pct * 1.37, 3)
                       for r in sub)
    a, _ = _gen(random.Random("w"), 1.37)
    b, _ = _gen(random.Random("w"), 1.37)
    assert a == b                                              # deterministic under both seeds


def test_the_session_record_and_factor():
    rng1, rec = NS.session_shaping("tw_100023")
    rng2, rec2 = NS.session_shaping("tw_100023")
    assert rec == rec2 and rng1.random() == rng2.random()
    assert NS.session_shaping("tw_100024")[1]["dl_factor"]["value"] != rec["dl_factor"]["value"]
    w, f = rec["walk"], rec["dl_factor"]
    assert w["space"] == "linear" and w["sigma_per_band"] == NS.WALK_SIGMA_PER_BAND and w["step_s"] == 2.0
    assert w["seed"] == "walk:0:tw_100023" and f["seed"] == "dl:0:tw_100023" and f["rule"] == NS.DL_FACTOR_RULE
    assert set(w["sigma_kbit_by_state"]) == set(SH.SCENARIOS) and 0.5 <= f["value"] <= 2.0
    assert f["value"] == NS.draw_dl_factor(random.Random("dl:0:tw_100023"))
    xs = [NS.draw_dl_factor(random.Random(f"x{i}")) for i in range(20000)]
    assert min(xs) >= 0.5 and max(xs) <= 2.0
    logs = sorted(math.log(x) for x in xs)
    assert abs(sum(logs) / len(logs)) < 0.02                                  # log-uniform: symmetric in log
    assert abs(sum(1 for x in logs if x < -math.log(2) / 2) / len(logs) - 0.25) < 0.015   # quartile at 2^-1/2
    assert "shaping" in inspect.signature(session_meta.start).parameters
    assert "**sched, **(shaping or {})" in inspect.getsource(session_meta.start)


def test_net_labels_and_the_wiring():
    names = [x.name for x in fields(NetLabel)]
    assert names == ["timestamp", "speed", "state", "delay_ms", "loss_pct", "redraw_kbit", "redraw_delay_ms",
                     "redraw_loss_pct", "walk_step", "apply_start", "apply", "lever",
                     "seam", "phase", "apply_rc"]               # v3 A9 apply instants, B4 lever, W's seam row, J's phase, rc
    assert NetLabel(1.0, 500, "2").delay_ms is None                           # the old three-field rows still build
    src = (HERE / "exp.py").read_text()
    assert "WALK_RNG, SHAPING_REC = session_shaping(session_dir.name)" in src
    assert 'SHAPER_OPTS.update(walk_rng=WALK_RNG, dl_factor=SHAPING_REC["dl_factor"]["value"])' in src
    assert src.index("SHAPER_OPTS.update(walk_rng=") > src.index("SHAPER_OPTS = {\"initial_state_fn\"")  # after the family's
    assert "profile_template=profile_template, shaping=SHAPING_REC)" in src
    for f in ("delay_ms=net_stat.delay_ms", "loss_pct=net_stat.loss_pct", "redraw_kbit=net_stat.redraw_kbit",
              "redraw_delay_ms=net_stat.redraw_delay_ms", "redraw_loss_pct=net_stat.redraw_loss_pct",
              "walk_step=net_stat.walk_step"):
        assert f in src, f
    assert "deadline = (ts if deadline is None else deadline) + net_stat.duration" in src
    assert "while time.time() < deadline:" in src


def test_check_session_reads_sub_steps():
    legacy = [{"timestamp": 0.0, "speed": 1000, "state": "5"}, {"timestamp": 20.0, "speed": 800, "state": "4"}]
    assert CS.substeps(legacy, 45.0) == [(legacy[0], 20.0, 1000), (legacy[1], 45.0, 800)]
    walked = [{"timestamp": 0.0, "speed": 1000, "state": "5", "walk_step": 0},
              {"timestamp": 2.0, "speed": 1200, "state": "5", "walk_step": 1},
              {"timestamp": 4.0, "speed": 1100, "state": "5", "walk_step": 2},
              {"timestamp": 12.0, "speed": 800, "state": "4", "walk_step": 0}]
    r = CS.substeps(walked, 14.0)
    assert [(a["timestamp"], end) for a, end, _ in r] == [(0.0, 12.0), (12.0, 14.0)]
    assert abs(r[0][2] - (1000 * 2 + 1200 * 2 + 1100 * 8) / 12) < 1e-9 and r[1][2] == 800


def _load(name):
    sp = importlib.util.spec_from_file_location(name, HERE.parent / "ops" / "diag" / f"{name}.py")
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def test_the_dry_run_plans_carry_the_walk():
    F = _load("fast_segment")
    fast = F.schedule("twitch", "tw1", 120.0, seed=0)
    base = F.schedule("twitch", "tw1", 120.0, seed=0, family="base")
    assert base == F.schedule("twitch", "tw1", 120.0, seed=0, family="base") and base != fast
    assert {e["matrix_id"] for e in base} == {"TW-M"} and {e["matrix_id"] for e in fast} == {"TW-M@fast"}
    for plan in (fast, base):
        assert any(e["walk_step"] >= 1 for e in plan) and len({e["dl_factor"] for e in plan}) == 1
        assert all(SH.SCENARIOS[e["state"]].rate_min <= e["rate_kbit"] <= SH.SCENARIOS[e["state"]].rate_max for e in plan)
    walks = [e for e in base if e["walk_step"] >= 1]
    assert all(abs(e["t"] - p["t"] - 2.0) < 2e-3 for p, e in zip(base, base[1:]) if e["walk_step"] >= 1)
    assert len(walks) > 40                                     # 120 s of 12-30 s re-draws: mostly walk steps
    try:
        F.schedule("twitch", "tw1", 30.0, family="slow")
        raise AssertionError("family slow accepted")
    except ValueError:
        pass
    T = _load("trio_dry_run")
    assert "family" in inspect.signature(T.fast_segment_phase).parameters
    assert "--walk-segment-s" in inspect.getsource(T)


def test_the_committed_simulation():
    raw = WS.FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == WS.WALK_SIM_SHA256
    import json
    t = json.loads(raw)
    assert t["walk"]["sigma_per_band"] == NS.WALK_SIGMA_PER_BAND and t["walk"]["space"] == "linear"
    for p, ms in t["matrices"].items():
        assert set(ms) == set(SH.PLATFORM_MATRICES[p])
        for m, v in ms.items():
            cb, ca = v["changes_per_session"]["before_walk"]["mean"], v["changes_per_session"]["after_walk"]["mean"]
            assert ca > cb and abs(sum(v["occupancy"].values()) - 1) < 1e-4, (p, m)


def test_a_walk_step_changes_the_htb_rate_only():
    """levi on 80af314: a walk step changes only the HTB rate (exp.py: make rate, both directions); netem changes only
    at the sub-step's re-draw (make slow); the dry run applies the same command forms."""
    import subprocess
    out = subprocess.run(["make", "-n", "rate", "BANDWIDTH=1234kbit", "IFB_INGRESS=lvi_tw1", "IFACE=eth9",
                          "EGRESS_CLASS=1:12"], cwd=HERE, capture_output=True, text=True).stdout.splitlines()
    assert out == ["sudo tc class change dev lvi_tw1 parent 1: classid 1:10 htb rate 1234kbit",
                   "sudo tc class change dev eth9 parent 1: classid 1:12 htb rate 1234kbit"], out
    src = (HERE / "exp.py").read_text()
    assert 'run(["make", "rate", f"BANDWIDTH={net_stat.rate}kbit"] + SLOT_VARS)' in src
    assert src.index("if net_stat.walk_step:") < src.index('"slow",')
    spec = importlib.util.spec_from_file_location("trio_dry_run_w", HERE.parent / "ops" / "diag" / "trio_dry_run.py")
    D = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(D)
    p = {"rate_kbit": 900, "delay_ms": 40, "loss_pct": 0.1}
    full = D.rate_cmds("eth9", "yt1", {**p, "walk_step": 0})
    walk = D.rate_cmds("eth9", "yt1", {**p, "walk_step": 3})
    assert sum("netem" in c for c in full) == (1 if D.NO_IFB else 2) and not any("netem" in c for c in walk)
    assert [c for c in full if "class" in c] == walk


def test_the_t2_clock_reads_the_redraw():
    """levi on 80af314 (2a): the stuck rule's T2 clock reads the sub-step's re-draw, so a walk dip below the rung
    margin does not reset it; rows without a re-draw (before §2) read their rate."""
    import stuck
    rung, floor = 1000.0, stuck.RUNG_MARGIN * 1000.0
    rows = [{"timestamp": 0.0, "speed": int(floor + 50), "state": "7", "redraw_kbit": int(floor + 50), "walk_step": 0},
            {"timestamp": 2.0, "speed": int(floor - 50), "state": "7", "redraw_kbit": int(floor + 50), "walk_step": 1},
            {"timestamp": 4.0, "speed": int(floor + 10), "state": "7", "redraw_kbit": int(floor + 50), "walk_step": 2}]
    assert stuck.above_since(rows, rung, 5.0) == 0.0                    # the dip at 2 s does not reset it
    old = [{k: v for k, v in r.items() if k in ("timestamp", "speed", "state")} for r in rows]
    assert stuck.above_since(old, rung, 5.0) == 4.0                     # no re-draw field: the row's rate, as before
    labs = [NetLabel(timestamp=r["timestamp"], speed=r["speed"], state=r["state"], redraw_kbit=r["redraw_kbit"],
                     walk_step=r["walk_step"]) for r in rows]
    assert stuck.above_since(labs, rung, 5.0) == 0.0


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_shaping_walk: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
