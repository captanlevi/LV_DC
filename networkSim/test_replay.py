#!/usr/bin/env python3
"""v3 W-A schedule replay (levi 17:1x: the instrumentation A/B's arms differ ONLY in the instrument flag, so every round
replays one fixed schedule): utils.netStat.replay_generator steps states 1 -> 10 -> 1, REPLAY_STEP_S each, with rate /
delay / loss and the walk drawn from generators seeded by the replay seed alone, so the same seed gives the same tc
changes whatever the session's name, the module's random state or the shaper options; exp.py --replay-schedule swaps the
generator and records meta shaper.replay, refused with --hold-state / --lever / steering; orchestrate --replay-schedule
is dry only (refused with --trio-record, --trio-steer, a lever window) and reaches every trio session.

    python3 test_replay.py        (exit 0 = pass)
"""
import json
import random
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import orchestrate as O  # noqa: E402
from utils import netStat as NS  # noqa: E402


def _run(seed, step=None, **opts):
    kw = {"step_s": step} if step else {}
    return [(x.state, x.rate, x.delay_ms, x.loss_pct, round(x.duration, 6), x.walk_step)
            for x in NS.replay_generator(60, {}, None, None, replay_seed=seed, **kw, **opts)]


def test_deterministic_and_fixed():
    random.seed(1)
    a = _run("wa-1")
    random.seed(999)                                        # the module's random state never matters
    b = _run("wa-1", walk_rng=random.Random("session-name"), dl_factor=1.7, max_consecutive_low=2)   # options ignored
    assert a == b and len(a) > 19
    assert _run("wa-2") != a                                # another seed, another schedule
    firsts = [s for s, *_ , ws in a if ws == 0]
    assert tuple(firsts) == NS.REPLAY_STATES and NS.REPLAY_STATES[0] == "1" and NS.REPLAY_STATES[9] == "10"
    assert NS.REPLAY_STATES[-1] == "1" and len(NS.REPLAY_STATES) == 19
    assert abs(sum(r[4] for r in a) - 19 * NS.REPLAY_STEP_S) < 1e-6
    assert abs(sum(r[4] for r in _run("wa-1", 40.0)) - 19 * 40.0) < 1e-6
    for s, rate, d, l, *_ in a:
        sc = NS.SCENARIOS[s]
        assert sc.rate_min <= rate <= sc.rate_max and sc.delay_min <= d <= sc.delay_max
    rec = NS.replay_record("wa-1")
    assert rec["session_s"] == round(19 * NS.REPLAY_STEP_S, 3) and rec["seed"] == "wa-1" and rec["states"][9] == "10"


def test_exp_wiring():
    src = (HERE / "exp.py").read_text()
    assert 'sys.exit("--replay-schedule excludes --hold-state, --lever and steering' in src
    assert "SHAPER_GEN = functools.partial(replay_generator, replay_seed=args.replay_schedule," in src
    assert '"replay": replay_record(args.replay_schedule' in src
    i_rep = src.index("if args.replay_schedule:              # v3 W-A")
    assert src.index("SHAPER_GEN = family_episode_generator") < i_rep          # the replay wins over a family


def test_orchestrate():
    dry_root = Path(tempfile.mkdtemp())
    denv = {"LVDC_DATA_ROOT_UUID": "0000-test", "LVDC_DATA_ROOT": str(dry_root)}
    base = ["--batch", "youtube=99,twitch=99,tiktok=99", "--concurrent", "--n-draw", "--window-min",
            str(O.RECORD_WINDOW_MIN), "--trio-yt-fullscreen", "--trio-layout", "composite", "--stratum-force", "unsteered"]
    r = lambda argv: O.arg_refusal(O.build_parser().parse_args(argv), denv)
    assert r(base + ["--replay-schedule", "wa-1"]) is None
    assert r(base + ["--replay-schedule", "wa-1", "--replay-step-s", "79"]) is None
    assert "only with --replay-schedule" in r(base + ["--replay-step-s", "79"])
    assert "outside [10, 600]" in r(base + ["--replay-schedule", "wa-1", "--replay-step-s", "5"])
    assert "needs steering off" in r(base + ["--replay-schedule", "wa-1", "--trio-steer"])
    lev = [a for a in base if a not in ("unsteered",)]
    lev[lev.index("--stratum-force") + 1:lev.index("--stratum-force") + 1] = ["lever"]
    assert "excludes a lever window" in r(lev + ["--replay-schedule", "wa-1"])
    rec = [a for a in base if a not in ("--stratum-force", "unsteered")] + ["--trio-record", "--expect-stratum", "unsteered"]
    got = O.arg_refusal(O.build_parser().parse_args(rec + ["--replay-schedule", "wa-1"]), denv)
    assert got and "dry only" in got, got
    src = (HERE / "orchestrate.py").read_text()
    assert 'cmd += ["--replay-schedule", REPLAY["seed"]]' in src and "if REPLAY and not hold:" in src


def main() -> int:
    test_deterministic_and_fixed()
    test_exp_wiring()
    test_orchestrate()
    print("test_replay: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
