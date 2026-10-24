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
    assert tuple(firsts) == NS.REPLAY_STATES and NS.REPLAY_STATES[0] == "10" and NS.REPLAY_STATES[9] == "1"
    assert NS.REPLAY_STATES[-1] == "10" and len(NS.REPLAY_STATES) == 19            # levi 17:21: startup in the top state
    assert NS.REPLAY_STATES == tuple(map(str, [10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]))
    assert abs(sum(r[4] for r in a) - 19 * NS.REPLAY_STEP_S) < 1e-6
    assert abs(sum(r[4] for r in _run("wa-1", 40.0)) - 19 * 40.0) < 1e-6
    for s, rate, d, l, *_ in a:
        sc = NS.SCENARIOS[s]
        assert sc.rate_min <= rate <= sc.rate_max and sc.delay_min <= d <= sc.delay_max
    rec = NS.replay_record("wa-1")
    assert rec["session_s"] == round(19 * NS.REPLAY_STEP_S, 3) and rec["seed"] == "wa-1" and rec["states"][9] == "1"


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
    # the window fit uses the replay's own length, not the drawn law's (else a long draw idles the slot under max-per-slot 1)
    assert 'slen = {**slen, "minutes": round(len(_ns.REPLAY_STATES) * (REPLAY.get("step_s") or _ns.REPLAY_STEP_S) / 60.0, 4),' in src
    assert src.index("if slen and REPLAY:") < src.index('if window and slen and not window_fits(time.time(), slen["minutes"]')
    assert abs(len(NS.REPLAY_STATES) * NS.REPLAY_STEP_S / 60.0 - 25.0167) < 1e-3


def test_yt_tt_pins():
    """levi 17:18 (W-A pins every platform; t59: drawn yt / tt channels left the A/B unresolvable): --trio-pin
    youtube:channel=@handle (a yt-list entry, no category) and tiktok:room=@user (opened directly, never rotated); the
    same dry-only rule as every pin (never --trio-record)."""
    k, pin = O.parse_pin("youtube:channel=@AlJazeeraEnglish", "W-A_fixed_channel")
    assert k == "youtube" and pin["channel"] == "@AlJazeeraEnglish"
    k, pin = O.parse_pin("tiktok:room=@stef_gamingg", "W-A_fixed_channel")
    assert k == "tiktok" and pin["room"] == "@stef_gamingg"
    assert O.parse_pin("twitch:category=Chess,channel=gothamchess", "r")[1]["channel"] == "gothamchess"
    for bad, why in (("youtube:channel=@NotOnTheList", "not on the yt list"), ("youtube:category=News", "twitch only"),
                     ("tiktok:channel=@x", "channel pins are twitch / youtube"), ("twitch:room=@x", "room pins are tiktok"),
                     ("tiktok:room=@bad/room", "not a tiktok @user"), ("twitch:channel=x", "names its category")):
        try:
            O.parse_pin(bad, "r")
            raise AssertionError(bad)
        except ValueError as e:
            assert why in str(e), (bad, str(e))
    saved = O.TT_ROOM_ROTATION_S
    try:
        O.TT_ROOM_ROTATION_S = 600
        cmd = O._collection_cmd("tiktok", None, 5, auto=True, tt_room=O.tiktok_room_url("@stef_gamingg"))
        assert cmd[cmd.index("--channel-url") + 1] == "https://www.tiktok.com/@stef_gamingg/live"
        assert "--tt-room-rotation" not in cmd                       # a pinned room is never rotated away
        assert "--tt-room-rotation" in O._collection_cmd("tiktok", None, 5, auto=True)
    finally:
        O.TT_ROOM_ROTATION_S = saved
    src = (HERE / "orchestrate.py").read_text()
    assert 'channel = yt_channels.by_handle(pin["channel"])' in src and 'tt_room = tiktok_room_url(pin["room"])' in src
    assert "tw_category=category, matrix=mid, tt_room=tt_room)" in src


def main() -> int:
    test_deterministic_and_fixed()
    test_exp_wiring()
    test_orchestrate()
    test_yt_tt_pins()
    print("test_replay: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
