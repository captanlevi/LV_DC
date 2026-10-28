"""W (levi 48888ba7 / 00171, supervisor 13:03): a chain session's schedule is generated up front and played from a
uniformly random episode boundary, wrapping at its end; the seam obeys the generator's own low-state cap (b'). The
flatness check runs the collector's own generators and wrap_schedule (N = 2000 per W-B config, +-0.03, truncated
sessions included)."""
import collections
import random
from pathlib import Path

import pytest

from utils import netStat as NS
from utils.dataModels import NetStat
from utils.shaping import (PLATFORM_MATRICES, draw_fast_episode_s, draw_session_minutes, schedule_of, session_episodes,
                           stationary_distribution)

HERE = Path(__file__).resolve().parent


class _K:
    """An offset rng stub: randrange returns k."""
    def __init__(self, k):
        self.k = k

    def randrange(self, n):
        return self.k % n


def _ep(state, ep, durs=(10.0,)):
    return [NetStat(rate=100, duration=d, delay_ms=1, loss_pct=0.0, state=state, walk_step=0, episode=ep) for d in durs]


def test_offset_zero_is_the_schedule_as_generated_and_rotation_keeps_every_change():
    items = _ep("5", 0) + _ep("6", 1) + _ep("7", 2)
    out, rec = NS.wrap_schedule(items, _K(0), max_consecutive_low=2)
    assert out == items and rec["offset_episode"] == 0 and rec["seam_index"] is None and rec["wrap_elapsed_s"] is None
    out, rec = NS.wrap_schedule(items, _K(1), max_consecutive_low=2)
    assert [x.state for x in out] == ["6", "7", "5"] and rec["schedule_offset_s"] == 10.0
    assert rec["seam_index"] == 2 and rec["wrap_elapsed_s"] == 20.0 and rec["seam_recovery_s"] == 0.0
    assert sorted(map(repr, out)) == sorted(map(repr, items))            # every change kept, only the order moves


def test_a_change_without_its_episode_is_refused():
    with pytest.raises(ValueError):
        NS.wrap_schedule([NetStat(rate=1, duration=1.0, delay_ms=1, loss_pct=0.0, state="5")], _K(0))


def test_generators_tag_episodes_and_the_offset_draw_never_touches_the_module_random():
    t = PLATFORM_MATRICES["youtube"]["B"][0]
    random.seed(7)
    a = list(NS.net_episode_generator(60, t, forced_initial_state="5", max_episodes=8, max_consecutive_low=6))
    assert [x.episode for x in a] == sorted(x.episode for x in a) and {x.episode for x in a} == set(range(8))
    random.seed(7)
    b = list(NS.net_episode_generator(60, t, forced_initial_state="5", max_episodes=8, max_consecutive_low=6))
    st = random.getstate()
    out, rec = NS.wrap_schedule(b, random.Random("wrap:yt_1"), max_consecutive_low=99)   # no seam recovery possible
    assert a == b and random.getstate() == st and rec["seam_recovery_s"] == 0.0


def test_base_seam_recovery_is_the_generators_own_cap_rule():
    """cap 3: a tail of 2 low episodes joins the head's 2: the 4th low episode in the joined run becomes one recovery
    episode at "4" (re-drawn in its band), the count restarts, and nothing else changes."""
    items = _ep("1", 0, (6.0, 4.0)) + _ep("2", 1) + _ep("6", 2) + _ep("3", 3) + _ep("2", 4)
    random.seed(1)
    out, rec = NS.wrap_schedule(items, _K(2), max_consecutive_low=3)
    states = [x.state for x in out]
    assert states == ["6", "3", "2", "1", "1", "4"]                  # head episode 0 kept (3rd low), episode 1 -> "4"
    four = [x for x in out if x.state == "4"]
    lo, hi = NS.SCENARIOS["4"].rate_min, NS.SCENARIOS["4"].rate_max
    assert all(lo <= x.redraw_kbit <= hi for x in four) and four[0].episode == 1
    assert rec["seam_recovery_s"] == 10.0 and sum(x.duration for x in out) == sum(x.duration for x in items)


def test_base_seam_inside_the_cap_needs_nothing():
    items = _ep("1", 0) + _ep("6", 1) + _ep("2", 2)
    out, rec = NS.wrap_schedule(items, _K(1), max_consecutive_low=3)
    assert rec["seam_recovery_s"] == 0.0 and [x.state for x in out] == ["6", "2", "1"]


def test_family_seam_recovery_cuts_at_the_seconds_cap_and_holds_4_to_the_episodes_end():
    """low_cap_s 30: 20 s of low tail joins a head episode of three 10 s low sub-steps: 10 s stay low, the remaining 20 s
    are "4" (two re-drawn sub-steps of their own lengths); the next episode is untouched."""
    items = _ep("2", 0, (10.0, 10.0, 10.0)) + _ep("7", 1, (10.0,)) + _ep("1", 2, (20.0,))
    random.seed(3)
    out, rec = NS.wrap_schedule(items, _K(1), low_cap_s=30.0)
    assert [x.state for x in out[:2]] == ["7", "1"]                    # episodes 1, 2, then the seam, then episode 0
    assert [(x.state, round(x.duration, 6)) for x in out[2:]] == [("2", 10.0), ("4", 10.0), ("4", 10.0)]
    assert rec["seam_recovery_s"] == 20.0 and rec["cap"] == {"seconds": 30.0}


def test_family_cut_inside_a_sub_step_keeps_its_low_part():
    items = _ep("2", 0, (25.0,)) + _ep("7", 1, (10.0,)) + _ep("1", 2, (20.0,))
    out, rec = NS.wrap_schedule(items, _K(1), low_cap_s=30.0)
    assert [(x.state, round(x.duration, 6)) for x in out[2:]] == [("2", 10.0), ("4", 15.0)]
    assert rec["seam_recovery_s"] == 15.0


def test_steering_draw_and_its_deficits_do_not_depend_on_w():
    """Steering's weights / start are drawn at session start from the table and the base matrix (exp.py calls steer before
    the shaper starts); W only reorders the schedule built afterwards from the steered matrix, so its occupancy (time per
    state) is the generated schedule's own whenever the seam needs no recovery."""
    import steering
    t = PLATFORM_MATRICES["youtube"]["B"][0]
    a = steering.steer("youtube", t, None, "yt_100001")
    random.seed(11)
    items = list(NS.net_episode_generator(60, a[0], forced_initial_state=a[1] or "5", max_episodes=20,
                                          max_consecutive_low=99))
    out, rec = NS.wrap_schedule(items, random.Random("wrap:yt_100001"), max_consecutive_low=99)
    b = steering.steer("youtube", t, None, "yt_100001")
    assert a[0] == b[0] and a[1] == b[1] and rec["seam_recovery_s"] == 0.0
    occ = lambda xs: {s: round(sum(x.duration for x in xs if x.state == s), 6) for s in {x.state for x in xs}}
    assert occ(out) == occ(items)


def test_exp_wraps_chain_sessions_only_and_flags_the_seam_row():
    src = (HERE / "exp.py").read_text()
    i = src.index("def _schedule(")
    body = src[i:src.index("\n\n\n", i)]
    assert "SHAPER_GEN not in (net_episode_generator, family_episode_generator)" in body       # replay never wrapped
    flat = " ".join(src.split())                                  # the dry hold (levi 03:19) never wraps either
    assert ('WRAP_SEED = (session_dir.name if args.hold_state is None and not args.replay_schedule and not DRY_HOLD '
            'else None)') in flat
    assert "seam=seam, apply_rc=rc)" in src and '"shaper", schedule_wrap=' in src


def _schedule_ns(**stubs):
    """exp._schedule's own source, exec'd with stubs (exp.py exits at import without the repo's .venv)."""
    import ast
    src = (HERE / "exp.py").read_text()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_schedule")
    ns = {"random": random, "wrap_schedule": NS.wrap_schedule, "net_episode_generator": NS.net_episode_generator,
          "family_episode_generator": NS.family_episode_generator, "SHAPER_OPTS": {"max_consecutive_low": 99},
          "SCHEDULE_WRAP": {}, **stubs}
    exec(compile(ast.Module([fn], []), "exp._schedule", "exec"), ns)
    return ns


def test_the_record_keeps_the_prewrap_start_and_an_interactive_jump_is_never_wrapped():
    """levi 13:29: (1) meta.shaper.schedule_wrap.start_state_prewrap = the schedule's first change before rotation (the
    start cross-check compares against it; start_applied is a random episode's by design); (2) a u / d jump plays as
    generated, counted, and leaves the session's record."""
    t = PLATFORM_MATRICES["youtube"]["B"][0]
    ns = _schedule_ns(WRAP_SEED="yt_100009", SHAPER_GEN=NS.net_episode_generator)
    kw = dict(episode_length=60, transition_dict=t, forced_initial_state="7", max_episodes=12)
    random.seed(5)
    out = list(ns["_schedule"](kw))
    rec = dict(ns["SCHEDULE_WRAP"])
    assert rec["applied"] and rec["start_state_prewrap"] == "7" and len(out) > 0
    random.seed(5)
    jump = list(ns["_schedule"]({**kw, "forced_initial_state": "2"}, wrap=False))
    random.seed(5)
    assert jump == list(NS.net_episode_generator(**{**kw, "forced_initial_state": "2"}))      # as generated
    after = ns["SCHEDULE_WRAP"]
    assert after["interactive_restarts"] == 1 and after["seam_index"] is None
    assert after["start_state_prewrap"] == "7" and after["offset_episode"] == rec["offset_episode"]
    src = (HERE / "exp.py").read_text()
    assert "_make_task(next_s, wrap=False)" in src and "), wrap=wrap)):" in src


# ---------------------------------------------------------------------------------- the flatness check (levi 00171 (4))
N_SIM, TOL = 2000, 0.03
WB_CONFIGS = [("youtube", "B", "Q_360p"), ("twitch", "current", "Q_480p"), ("twitch", "TW-M@slow", "Q_480p"),
              ("tiktok", "TT-M@fast", None)]


def _flat(platform, mid, cls):
    import coverage_report as C
    tr, cap, init = PLATFORM_MATRICES[platform][mid]
    fam = mid.partition("@")[2]
    pi = stationary_distribution(tr)
    states = sorted(pi, key=int)
    band = [s for s in states if s in set(C.class_band(platform, cls)["band"])] if cls else states
    bands = {c: set(C.class_band(platform, c)["band"]) for c in C.RUNGS[platform]}
    saved = random.getstate()
    random.seed(f"wrapcheck:{platform}:{mid}")
    acc = {"full": (collections.Counter(), collections.Counter()), "trunc": (collections.Counter(), collections.Counter())}
    try:
        for i in range(N_SIM):
            key = f"wrapcheck:{platform}:{mid}:{i}"
            m = draw_session_minutes(key)["minutes"]
            fe = draw_fast_episode_s(key)["episode_s"] if fam == "fast" else None
            sc = schedule_of(mid, cap, m, episode_s=fe)
            eps = session_episodes(m, mid, fe)
            st0 = random.choices(band, weights=[pi[s] for s in band])[0]
            if fam:
                items = list(NS.family_episode_generator(sc["episode_s"], tr, sc["redraw_s"], sc["low_cap_s"],
                                                         sc["self_loops"], forced_initial_state=st0, max_episodes=eps))
                out, _ = NS.wrap_schedule(items, random.Random(key), low_cap_s=sc["low_cap_s"])
            else:
                items = list(NS.net_episode_generator(int(sc["episode_s"]), tr, forced_initial_state=st0,
                                                      max_episodes=eps, max_consecutive_low=cap))
                out, _ = NS.wrap_schedule(items, random.Random(key), max_consecutive_low=cap)
            S = sum(x.duration for x in out)
            for name, T in (("full", S), ("trunc", S * random.uniform(0.3, 1.0))):
                num, den = acc[name]
                t = 0.0
                for x in out:
                    a, b = t, min(t + x.duration, T)
                    t += x.duration
                    if a >= T:
                        break
                    for q in range(5):
                        ov = max(0.0, min(b, (q + 1) * T / 5) - max(a, q * T / 5))
                        if ov:
                            den[q] += ov
                            num[("low", q)] += ov * (int(x.state) <= 4)
                            for c, bs in bands.items():
                                num[(c, q)] += ov * (x.state in bs)
    finally:
        random.setstate(saved)
    worst = {}
    for name, (num, den) in acc.items():
        for c in ["low"] + list(bands):
            v = [num[(c, q)] / den[q] for q in range(5)]
            mu = sum(v) / 5
            worst[(name, c)] = max(abs(x - mu) for x in v)
    return worst


@pytest.mark.parametrize("platform,mid,cls", WB_CONFIGS)
def test_w_is_flat_by_elapsed_quintile(platform, mid, cls):
    worst = _flat(platform, mid, cls)
    bad = {k: round(v, 4) for k, v in worst.items() if v > TOL}
    assert not bad, bad


def main() -> int:
    test_offset_zero_is_the_schedule_as_generated_and_rotation_keeps_every_change()
    try:
        test_a_change_without_its_episode_is_refused()
    except BaseException as e:            # pytest.raises outside pytest still raises its own failure type
        raise SystemExit(f"refusal test failed: {e!r}")
    test_generators_tag_episodes_and_the_offset_draw_never_touches_the_module_random()
    test_base_seam_recovery_is_the_generators_own_cap_rule()
    test_base_seam_inside_the_cap_needs_nothing()
    test_family_seam_recovery_cuts_at_the_seconds_cap_and_holds_4_to_the_episodes_end()
    test_family_cut_inside_a_sub_step_keeps_its_low_part()
    test_steering_draw_and_its_deficits_do_not_depend_on_w()
    test_exp_wraps_chain_sessions_only_and_flags_the_seam_row()
    test_the_record_keeps_the_prewrap_start_and_an_interactive_jump_is_never_wrapped()
    for c in WB_CONFIGS:
        test_w_is_flat_by_elapsed_quintile(*c)
    print("test_schedule_wrap: ok")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
