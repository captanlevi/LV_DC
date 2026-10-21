#!/usr/bin/env python3
"""Label-diversity steering (prereg d32a48ce, item 2): steering.steer draws a session's dwell weights over the shaper
states at session start from the deficit table and the base matrix only. Deficits, the pull, the 10 % floor, met
targets pulling nothing, the extreme (one unmet tier), the fallback without a table, the seeded start state, and the
gate's static proof that nothing in the draw reads the running session (the player, the page, the labels).

    python3 test_steering.py        (exit 0 = pass)
"""
import ast
import inspect
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import steering as S  # noqa: E402
from utils.shaping import TWITCH_TRANSITIONS, stationary_distribution  # noqa: E402

H = 3600.0


def table(tiers=None, stall=0.0, by_state=None, pf="twitch"):
    return {"per_platform": {pf: {"tiers": tiers or {}, "stall_s": stall, "by_state": by_state or {}}},
            "built_at": "t", "code": "c"}


def write(t):
    d = Path(tempfile.mkdtemp())
    (d / S.TABLE_NAME).write_text(json.dumps(t))
    return d / S.TABLE_NAME


def test_constants():
    assert (S.FLOOR, S.TIER_TARGET_S, S.STALL_TARGET_S) == (0.10, 7200.0, 3600.0)
    assert S.TARGETS_S["tiktok"] == {"Q_360p": 7200.0, "Q_480p": 7200.0, "Q_720p": 7200.0, "Q_1080p": 7200.0,
                                     "STALL": 3600.0}


def test_deficits_and_weights():
    t = table(tiers={"Q_144p": 3 * H, "Q_360p": H, "Q_480p": 2 * H, "Q_720p": 0.0, "Q_1080p": 0.0}, stall=0.5 * H,
              by_state={"1": {"STALL": 100.0, "Q_144p": 100.0}, "5": {"Q_480p": 100.0},
                        "9": {"Q_720p": 50.0, "Q_1080p": 50.0}})
    d = S.deficits(t, "twitch")
    assert d == {"Q_144p": 0.0, "Q_360p": 0.5, "Q_480p": 0.0, "Q_720p": 1.0, "Q_1080p": 1.0, "STALL": 0.5}, d
    w, pull = S.state_weights(t, "twitch", ["1", "5", "9", "10"])
    assert pull["1"] == 0.25 and pull["5"] == 0.0 and pull["9"] == 1.0 and abs(pull["10"] - 1.25 / 3) < 1e-9
    assert w["9"] == 1.0 and w["5"] == S.FLOOR and abs(w["1"] - (0.1 + 0.9 * 0.25)) < 1e-6   # floor, met pulls nothing
    assert all(S.FLOOR <= v <= 1.0 for v in w.values())
    # every target met: no pull anywhere, every weight 1 (the base schedule)
    met = table(tiers={c: 9 * H for c in S.TIERS["twitch"]}, stall=9 * H, by_state={"1": {"STALL": 1.0}})
    assert set(S.state_weights(met, "twitch", ["1", "2"])[0].values()) == {1.0}


def test_extreme_single_unmet_tier():
    """The gate's extreme: one unmet tier -> the states delivering it at 1, the rest at the floor, the steered matrix's
    stationary mass moved toward them."""
    states = sorted(TWITCH_TRANSITIONS, key=int)
    t = table(tiers={c: 9 * H for c in S.TIERS["twitch"] if c != "Q_1080p"}, stall=9 * H,
              by_state={s: ({"Q_1080p": 10.0} if s in ("9", "10") else {"Q_360p": 10.0}) for s in states})
    w, _ = S.state_weights(t, "twitch", states)
    assert w["9"] == w["10"] == 1.0 and all(w[s] == S.FLOOR for s in states if s not in ("9", "10")), w
    steered = S.steer_matrix(TWITCH_TRANSITIONS, w)
    assert all(abs(sum(r.values()) - 1.0) < 1e-9 for r in steered.values())
    pi0, pi1 = stationary_distribution(TWITCH_TRANSITIONS), stationary_distribution(steered)
    assert pi1["9"] + pi1["10"] > pi0["9"] + pi0["10"], (pi0, pi1)
    # the floor: no state's transitions-in drop below FLOOR x base before renormalising
    for s, row in TWITCH_TRANSITIONS.items():
        for tt, p in row.items():
            assert p * w[tt] >= S.FLOOR * p - 1e-12


def test_steer_and_fallback():
    states = sorted(TWITCH_TRANSITIONS, key=int)
    t = table(tiers={}, stall=0.0, by_state={s: {"STALL": 1.0} if s in ("1", "2") else {"Q_720p": 1.0} for s in states})
    p = write(t)
    m, start, rec = S.steer("twitch", TWITCH_TRANSITIONS, p, "tw_100090")
    assert rec["applied"] and rec["table"]["sha256"] and rec["table"]["path"] == str(p) and rec["seed"] == "tw_100090"
    assert set(rec["weights"]) == set(states) and start in states and rec["start_state"] == start
    assert S.steer("twitch", TWITCH_TRANSITIONS, p, "tw_100090")[1] == start                 # seeded by the session
    m2, s2, r2 = S.steer("twitch", TWITCH_TRANSITIONS, str(p) + ".missing", "x")
    assert m2 is TWITCH_TRANSITIONS and s2 is None and not r2["applied"] and r2["table"]["error"], r2
    m3, s3, r3 = S.steer("twitch", TWITCH_TRANSITIONS, None, "x")
    assert m3 is TWITCH_TRANSITIONS and r3["fallback"].startswith("base schedule")
    bad = write({"nope": 1})
    assert not S.steer("twitch", TWITCH_TRANSITIONS, bad, "x")[2]["applied"]


def test_no_within_session_input():
    """The gate: the draw reads the table and the base matrix only. steer's parameters are (platform, transition,
    table_path, seed); steering.py imports nothing that touches the player, the page, the labels or the browser; and
    exp.py draws before the browser exists, never reassigning the matrix afterwards."""
    assert list(inspect.signature(S.steer).parameters) == ["platform", "transition", "table_path", "seed"]
    tree = ast.parse((HERE / "steering.py").read_text())
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
           {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= {"__future__", "hashlib", "json", "os", "random", "pathlib", "utils.shaping"}, mods
    src = (HERE / "steering.py").read_text().split('"""', 2)[2]                    # the code, not the docstring
    for word in ("browser", "page", "player", "labels.csv", "net_labels", "CDP", "eval("):
        assert word not in src, word
    exp = (HERE / "exp.py").read_text()
    i_steer = exp.index("steering.steer(platform, transition")
    assert i_steer < exp.index("browser = ") or "browser = " not in exp[:i_steer]
    after = exp[i_steer + 1:]
    assert not any(ln.strip().startswith("transition =") or ln.strip().startswith("transition, ")
                   for ln in after.splitlines()), "the matrix is reassigned after the draw"


def test_balancer_targets():
    """Item 5: with TARGET_MINUTES (set by --trio-steer) the balancer's class deficits are the targets' missing counted
    minutes, ranked by share; a met class pulls nothing; unset, Addendum Q's session floors as before."""
    import coverage_report as CR
    snap = {"platform": "twitch", "classes": {"STALL": {"sessions_ge_min": 9, "minutes": 30.0},
                                             "Q_360p": {"sessions_ge_min": 9, "minutes": 150.0},
                                             "Q_1080p": {"sessions_ge_min": 9, "minutes": 0.0}}}
    saved = CR.TARGET_MINUTES
    try:
        CR.TARGET_MINUTES = {p: {c: v / 60.0 for c, v in t.items()} for p, t in S.TARGETS_S.items()}
        d = CR.deficits(snap)
        assert [(x["key"], x["share"]) for x in d] == [("Q_1080p", 1.0), ("STALL", 0.5)], d   # Q_360p met: no pull
        CR.TARGET_MINUTES = None
        assert CR.deficits(snap) == []                                           # session floors (all >= 5): none
    finally:
        CR.TARGET_MINUTES = saved


def main() -> int:
    test_constants()
    test_deficits_and_weights()
    test_extreme_single_unmet_tier()
    test_steer_and_fallback()
    test_no_within_session_input()
    test_balancer_targets()
    print("test_steering: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
