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
    # peter 16:43: a table naming tiers outside VideoState's names ("144p") is refused, never read as 0 seconds
    named = write(table(tiers={"144p": 9 * H}, by_state={"1": {"144p": 1.0}}))
    r = S.steer("twitch", TWITCH_TRANSITIONS, named, "x")[2]
    assert not r["applied"] and "class names" in r["table"]["error"], r
    exp = (HERE / "exp.py").read_text()
    assert "steering_fallback=" in exp                                             # levi 16:43: logged in meta


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


def test_steer_platforms():
    """levi 20:58: --trio-steer-platforms youtube,twitch. An unlisted platform (tt) gets no table (exp.py never calls
    steer: base matrix, base start rule), LVDC_STEER_SKIPPED -> meta steering_fallback "platform not steered", and no
    TARGET_MINUTES (its balancer deficits are exactly a no-steer session's); listed platforms are as at 057760cd."""
    import coverage_report as CR
    import orchestrate as O
    saved = (O.TRIO_STEER, O.TRIO_STEER_PLATFORMS, CR.TARGET_MINUTES, dict(O.COVERAGE_ALLOWED))
    try:
        O.TRIO_STEER, O.TRIO_STEER_PLATFORMS = False, None
        assert O.steer_env("tiktok", "/r") == {}
        O.TRIO_STEER = True
        assert O.steer_env("tiktok", "/r") == {"LVDC_STEER_TABLE": "/r/_summaries/" + S.TABLE_NAME}   # no list: all
        O.TRIO_STEER_PLATFORMS = {"youtube", "twitch"}
        assert O.steer_env("tiktok", "/r") == {"LVDC_STEER_SKIPPED": "platform not steered"}
        assert O.steer_env("twitch", "/r") == {"LVDC_STEER_TABLE": "/r/_summaries/" + S.TABLE_NAME}
        # the balancer: tt's deficits under the switch == a no-steer session's; yt's == the all-platform steering's
        snap = lambda pf: {"platform": pf, "classes": {c: {"sessions_ge_min": 2, "minutes": 30.0}  # noqa: E731
                                                        for c in S.TARGETS_S[pf]}}
        CR.TARGET_MINUTES = None
        base_tt = CR.deficits(snap("tiktok"))
        O.COVERAGE_ALLOWED.clear()
        O.steer_balancer(None)
        all_yt, all_allowed = CR.deficits(snap("youtube")), dict(O.COVERAGE_ALLOWED)
        O.COVERAGE_ALLOWED.clear()
        O.steer_balancer({"youtube", "twitch"})
        assert "tiktok" not in CR.TARGET_MINUTES and CR.deficits(snap("tiktok")) == base_tt
        assert CR.deficits(snap("youtube")) == all_yt and dict(O.COVERAGE_ALLOWED) == all_allowed   # TW-H as signed
        O.COVERAGE_ALLOWED.clear()
        O.steer_balancer({"youtube"})
        assert "twitch" not in O.COVERAGE_ALLOWED                                 # twitch unsteered: no TW-H opt-in
    finally:
        O.TRIO_STEER, O.TRIO_STEER_PLATFORMS, CR.TARGET_MINUTES = saved[:3]
        O.COVERAGE_ALLOWED.clear(); O.COVERAGE_ALLOWED.update(saved[3])
    # exp.py: the draw needs LVDC_STEER_TABLE (absent for an unlisted platform), and the skip is logged in meta
    exp = (HERE / "exp.py").read_text()
    assert 'if os.environ.get("LVDC_STEER_TABLE") is not None and args.hold_state is None and TRACE is None:' in exp
    assert "_steering.fallback_of(os.environ, args.hold_state, STEERING)" in exp



def test_every_drawable_state_is_an_accepted_start():
    """w9 (levi 02:41, shagun's stop-and-read): every state of every platform's matrix, opt-in included (TW-H's 11), is
    an accepted exp.py --initial-state: the balancer's band start (coverage_report.initial_state_forced), the steered
    start (steering.start_state) and a pin draw only from these; exp's choices are utils.shaping.state_ids()."""
    import coverage_report as CR
    from utils.shaping import PLATFORM_MATRICES, SCENARIOS, state_ids
    ids = set(state_ids())
    assert ids == set(SCENARIOS) and "11" in ids
    for p, ms in PLATFORM_MATRICES.items():
        for m, v in ms.items():
            t = v[0]
            states = set(t) | {d for row in t.values() for d in row}
            assert states <= ids, (p, m, states - ids)
            pi = stationary_distribution(t)                       # initial_state_forced draws from these
            assert {s for s, x in pi.items() if x > 0} <= ids, (p, m)
    assert ("twitch", "TW-H") in CR.OPT_IN_MATRICES
    tree = ast.parse((HERE / "exp.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant)
             and n.args[0].value in ("--initial-state", "--hold-state")]
    init = next(c for c in calls if c.args[0].value == "--initial-state")
    assert any(k.arg == "choices" and ast.unparse(k.value) == "all_states" for k in init.keywords)
    assert "all_states = state_ids()" in (HERE / "exp.py").read_text()
    assert "range(1, 11)" not in (HERE / "exp.py").read_text()
    from utils.shaping import TWITCH_MATRICES, start_state_refusal          # a start outside the matrix: refused early
    assert start_state_refusal("11", TWITCH_MATRICES["TW-H"][0], "TW-H") is None
    assert "not a state of matrix TW-M" in start_state_refusal("11", TWITCH_MATRICES["TW-M"][0], "TW-M")
    assert start_state_refusal(None, TWITCH_MATRICES["TW-M"][0], "TW-M") is None
    assert "_bad = start_state_refusal(args.initial_state, transition, matrix_id)" in (HERE / "exp.py").read_text()

def test_fallback_of():
    """Every non-steered session names why (levi 16:43 / 20:58 / 00:41): unlisted platform, a held (pinned) state under
    a table, a table that did not apply (its error, else its fallback, else "not applied"); steered or steering off: None."""
    tab = {"LVDC_STEER_TABLE": "/r/t.json"}
    assert S.fallback_of({}, None, None) is None and S.fallback_of({}, "1", None) is None          # steering off
    assert S.fallback_of({"LVDC_STEER_SKIPPED": "platform not steered"}, None, None) == "platform not steered"
    assert S.fallback_of({"LVDC_STEER_SKIPPED": "platform not steered"}, "1", None) == "platform not steered"
    assert S.fallback_of(tab, "1", None) == S.PINNED == "pinned"
    assert S.fallback_of(tab, None, {"applied": True}) is None
    assert S.fallback_of(tab, None, {"applied": False, "table": {"error": "missing"}}) == "missing"
    assert S.fallback_of(tab, None, {"applied": False, "table": {}, "fallback": "no deficit"}) == "no deficit"
    assert S.fallback_of(tab, None, {"applied": False}) == "not applied"


def main() -> int:
    test_constants()
    test_deficits_and_weights()
    test_fallback_of()
    test_every_drawable_state_is_an_accepted_start()
    test_extreme_single_unmet_tier()
    test_steer_and_fallback()
    test_no_within_session_input()
    test_balancer_targets()
    test_steer_platforms()
    print("test_steering: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
