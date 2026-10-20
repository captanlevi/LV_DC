#!/usr/bin/env python3
"""LVQoE prereg Addendum Q am. 17 (2026-10-20, levi at LVQoE 67bc6b3e; TT-M's cap corrected after it): the slow and fast
schedule families on each platform's default stationary matrix. Covered:
  - the ids ("<m>@slow|fast") on the base's tuple, their schedules, episodes holding the session time, the low-state cap
    in seconds = the base cap x 60 s (TW-M 120, A 360, TT-M 240);
  - the generator: exact episodes, no self-loops (parameters at LVQoE 3c409b93), the cap in seconds (a low episode cut at the cap and held
    at state 4 for the rest of it), no sub-step under 1 s;
  - the committed occupancy (schedule_occupancy.py): its sha256 is OCCUPANCY_SHA256 and a fresh seeded simulation
    reproduces it; @slow's occupancy is not the base's stationary mass;
  - the steering: a family qualifies on its own occupancy (against FLOOR_MINUTES / its session's minutes); every
    deficit ranked by share; a family's floor met by drawing the family; siblings reserve families too; young families
    are explored; a family outside the allowed set has no floor;
  - the record: shaper fields from session_meta.shaper_config, check_session's cap in seconds; orchestrate's episodes.
Synthetic; no browser.

    python3 test_schedule_families.py        (exit 0 = pass)
"""
import hashlib
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as CS  # noqa: E402
import coverage_report as C  # noqa: E402
import schedule_occupancy as SO  # noqa: E402
import session_meta  # noqa: E402
from utils import netStat as NS, shaping as SH  # noqa: E402

FAMILIES = {"twitch": ("TW-M", 2, 23), "youtube": ("A", 6, 21), "tiktok": ("TT-M", 4, 23)}


def _run(matrix_id, platform, seed=0, transitions=None, forced=None, episodes=None):
    t, cap, _ = SH.PLATFORM_MATRICES[platform][matrix_id]
    sc = SH.schedule_of(matrix_id, cap, FAMILIES[platform][2])
    random.seed(seed)
    eps = episodes or SH.session_episodes(FAMILIES[platform][2], matrix_id)
    rows = list(NS.family_episode_generator(sc["episode_s"], transitions or t, sc["redraw_s"], sc["low_cap_s"],
                                            sc["self_loops"], forced_initial_state=forced, max_episodes=eps,
                                            initial_state_fn=SH.initial_state_stationary))
    return rows, sc, eps


def _episodes(rows, episode_s):
    """[[sub-steps of episode k]] by cumulative time (episodes are exact)."""
    out, t = [[]], 0.0
    for r in rows:
        if t >= episode_s * len(out) - 1e-6:
            out.append([])
        out[-1].append(r)
        t += r.duration
    return out


def test_ids_schedules_and_episodes():
    for p, (base, cap, eps) in FAMILIES.items():
        assert SH.FAMILY_BASE[p] == base
        for fam in ("slow", "fast"):
            mid = f"{base}@{fam}"
            assert SH.PLATFORM_MATRICES[p][mid] == SH.PLATFORM_MATRICES[p][base]          # the base's tuple
            sc = SH.schedule_of(mid, cap, eps)
            assert sc["schedule"] == fam and sc["base_matrix"] == base and sc["low_cap_s"] == cap * 60
            assert sc["self_loops"] is False                                              # parameters 3c409b93
            # the session time held: @slow 3 episodes of session / 3, @fast 12 s episodes
            assert abs(SH.session_episodes(eps, mid) * sc["episode_s"] - eps * 60) <= (0 if fam == "slow" else 6)
        assert SH.schedule_of(base, cap)["schedule"] == "base" and SH.schedule_of(base, cap)["episode_s"] == 60
    assert SH.schedule_of("TW-M@slow", 2, 23)["episode_s"] == 460 and SH.schedule_of("A@slow", 6, 21)["episode_s"] == 420
    assert SH.schedule_of("A@slow", 6, 21)["low_cap_s"] == 360 and SH.schedule_of("TT-M@fast", 4)["low_cap_s"] == 240
    assert SH.schedule_of("TW-M@slow", 2, 23)["redraw_s"] == [12.0, 30.0] and SH.schedule_of("TW-M@fast")["redraw_s"] == [2.4, 6.0]
    assert [SH.session_episodes(23, m) for m in ("TW-M", "TW-M@slow", "TW-M@fast")] == [23, 3, 115]
    assert [SH.session_episodes(21, m) for m in ("A", "A@slow", "A@fast")] == [21, 3, 105]
    assert SH.schedule_of("hold:5")["schedule"] == "base" and SH.schedule_of(None)["schedule"] == "base"
    for bad in ("TW-M@medium", "TW-M@slow"):          # an unknown schedule; @slow without the base episodes
        try:
            SH.schedule_of(bad)
            raise AssertionError(f"{bad} must raise")
        except ValueError:
            pass


def test_generator_exact_episodes_no_self_loops_and_the_cap():
    for p, (base, cap, _) in FAMILIES.items():
        for fam in ("slow", "fast"):
            for seed in range(5):
                rows, sc, eps = _run(f"{base}@{fam}", p, seed)
                assert abs(sum(r.duration for r in rows) - eps * sc["episode_s"]) < 1e-6
                assert min(r.duration for r in rows) >= 1.0 - 1e-9
                run = longest = 0.0
                for r in rows:
                    run = run + r.duration if int(r.state) <= 3 else 0.0
                    longest = max(longest, run)
                assert longest <= sc["low_cap_s"] + 1e-6, (p, fam, longest)
                ep = _episodes(rows, sc["episode_s"])
                assert len(ep) == eps
                # no self-loop (both families): each episode starts away from the last one's end
                assert all(b[0].state != a[-1].state for a, b in zip(ep, ep[1:])), [[r.state for r in e] for e in ep]


def test_a_low_episode_is_cut_at_the_cap_and_held_at_4():
    low_only = {"2": {"3": 1.0}, "3": {"2": 1.0}, "4": {"2": 1.0}}   # every draw is low: only the cap leaves it
    rows, sc, _ = _run("TW-M@slow", "twitch", transitions=low_only, forced="2")
    ep = _episodes(rows, 460)
    for e in ep:                                          # 120 s low, then the other 340 s of the episode at 4
        low = sum(r.duration for r in e if r.state == "2")
        held = sum(r.duration for r in e if r.state == "4")
        assert abs(low - 120) < 1e-6 and abs(held - 340) < 1e-6 and [r.state for r in e] == sorted(
            [r.state for r in e], key=lambda s: s != "2"), [(r.state, r.duration) for r in e]
    rows, sc, _ = _run("TW-M@fast", "twitch", transitions=low_only, forced="2", episodes=22)
    states = [e[0].state for e in _episodes(rows, 12)]
    assert states == ["2", "3"] * 5 + ["4"] + ["2", "3"] * 5 + ["4"], states     # 10 x 12 s = 120 s, then one at 4
    rows, _, _ = _run("A@slow", "youtube", transitions=low_only, forced="2", episodes=2)
    for e in _episodes(rows, 420):                        # yt A's cap is 360 s: 360 s low, the last 60 s at 4
        assert abs(sum(r.duration for r in e if r.state == "2") - 360) < 1e-6
        assert abs(sum(r.duration for r in e if r.state == "4") - 60) < 1e-6


def test_the_committed_occupancy():
    raw = SO.FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SO.OCCUPANCY_SHA256
    t = json.loads(raw)
    assert {p: sorted(v) for p, v in t["families"].items()} == {
        "tiktok": ["TT-M@fast", "TT-M@slow"], "twitch": ["TW-M@fast", "TW-M@slow"], "youtube": ["A@fast", "A@slow"]}
    for p, fams in t["families"].items():
        for m, v in fams.items():
            assert abs(sum(v["occupancy"].values()) - 1.0) < 1e-4
            # restart §4 / §2b: drawn lengths, U(10, 40) min; @slow 3 episodes, @fast 12-48 s episodes
            assert 10.0 <= v["session_minutes"]["min"] and v["session_minutes"]["max"] <= 40.0
            if m.endswith("@slow"):
                assert v["episodes_per_session"]["min"] == v["episodes_per_session"]["max"] == 3
            else:
                assert 12.0 <= v["episode_s"]["min"] and v["episode_s"]["max"] <= 48.0
                assert v["episodes_per_session"]["min"] >= 12 and v["episodes_per_session"]["max"] <= 200
    pi = SH.stationary_distribution(SH.TWITCH_TRANSITIONS_M)
    occ = SO.occupancy("twitch", "TW-M@slow")
    assert sum(abs(occ[s] - pi[s]) for s in pi) > 0.05          # without self-loops the occupancy is not pi
    assert SO.dumps(SO.table()).encode() == raw, "the simulation no longer reproduces the fixture"
    state = random.getstate()
    SO.simulate("twitch", "TW-M@slow", sessions=3)
    assert random.getstate() == state                            # the global random state is restored


def test_a_family_qualifies_on_its_own_occupancy():
    tw = SH.PLATFORM_MATRICES["twitch"]
    b = C.band_map("twitch", tw, sorted(tw))
    for cls, v in b.items():
        for m in ("TW-M@slow", "TW-M@fast"):
            mass = round(sum(p for s, p in SO.occupancy("twitch", m).items() if s in v["band"]), 4)
            assert v["mass"][m] == mass
            thr = C.FLOOR_MINUTES / C.session_minutes("twitch", m)
            assert v["min_mass_family"][m] == round(thr, 4) and ((m in v["qualifies"]) == (mass >= thr and mass > 0))
        assert v["mass"]["TW-M"] == round(sum(p for s, p in SH.stationary_distribution(tw["TW-M"][0]).items()
                                              if s in v["band"]), 4)
    # restart §4 (levi): every platform and schedule qualifies on the expected session minutes, the mean of U(10, 40)
    assert C.session_minutes("twitch", "TW-M@slow") == C.session_minutes("youtube", "A@slow") == 25.0
    assert C.session_minutes("twitch", "TW-M") == 25.0


def _snap(classes_short: dict, per_matrix: dict | None = None):
    snap = {"platform": "twitch", "classes": {c: {"sessions_ge_min": 5, "minutes": 50} for c in C.CLASSES["twitch"]},
            "per_matrix": per_matrix or {}, "stall_share_pooled": 0.1}
    for c, n in classes_short.items():
        snap["classes"][c] = {"sessions_ge_min": n, "minutes": 9}
    return snap


def test_deficits_ranked_by_share_and_family_floors():
    tw = SH.PLATFORM_MATRICES["twitch"]
    snap = _snap({"Q_360p": 3}, {"TW-M@slow": {"sessions": 3}, "TW-M@fast": {"sessions": 1}})
    d = C.deficits(snap, families=["TW-M@slow", "TW-M@fast"])
    assert [(x["kind"], x["key"], x["share"]) for x in d] == [
        ("schedule", "TW-M@fast", 0.8), ("class", "Q_360p", 0.4), ("schedule", "TW-M@slow", 0.4)], d
    r = C.pick_matrix(snap, tw)
    assert r["target"]["key"] == "TW-M@fast" and r["matrix"] == "TW-M@fast" and r["why"] == "schedule floor 5", r
    assert C.initial_state_forced(r, tw) is None                 # a schedule target keeps the matrix's start rule
    r = C.pick_matrix(snap, tw, reserved={"TW-M@fast": 3})       # siblings hold three @fast sessions: 4 - 3 = 1
    assert r["target"]["key"] in ("Q_360p", "TW-M@slow") and r["reserved"] == {"TW-M@fast": 3}, r
    assert [x for x in r["deficits"] if x["key"] == "TW-M@fast"][0]["share"] == 0.2
    r = C.pick_matrix(snap, tw, ["TW-M", "TW-L", "current"])     # no family allowed: no family floor
    assert all(x["kind"] != "schedule" for x in r["deficits"]) and r["target"]["key"] == "Q_360p", r
    full = {m: {"sessions": 5} for m in ("TW-M@slow", "TW-M@fast")}
    assert C.deficits(_snap({}, full), families=list(full)) == []


def test_young_families_are_explored_for_a_class_target():
    tw = SH.PLATFORM_MATRICES["twitch"]
    pm = {m: {"sessions": 5, "tier_sessions": 5, "stall_onsets_mean": 1.0, "stall_share": 0.1,
              "class_minutes_mean": dict.fromkeys(C.CLASSES["twitch"], 1.0)} for m in ("TW-M", "TW-L", "current")}
    pm.update({m: {"sessions": 5, "tier_sessions": 1, "stall_onsets_mean": 1.0, "stall_share": 0.1,
                   "class_minutes_mean": dict.fromkeys(C.CLASSES["twitch"], 1.0)} for m in ("TW-M@slow", "TW-M@fast")})
    snap = _snap({"Q_480p": 2}, pm)
    snap["attempts"] = {"per_matrix": {"TW-M@slow": 4, "TW-M@fast": 2}, "total": 30, "recent": [["TW-M", True]]}
    r = C.pick_matrix(snap, tw)
    assert r["target"]["key"] == "Q_480p" and r["explore"]["young"] == ["TW-M@fast", "TW-M@slow"], r.get("explore")
    assert r["matrix"] == "TW-M@fast", r                         # the fewest attempts


def test_the_report_shows_each_familys_q1080p_minutes():
    """levi on 198f541: the coverage report lists per family (and its base) the Q_1080p minutes per tier session."""
    pm = {m: {"sessions": n, "tier_sessions": n, "class_minutes_mean": {"Q_1080p": q}}
          for m, n, q in (("TW-M", 6, 0.4), ("TW-M@fast", 3, 1.25), ("TW-M@slow", 2, 0.0), ("TW-L", 4, 0.0))}
    snap = {**_snap({}, pm), "n_sessions": 15, "stall_onsets_pooled": 0, "stall_run_s": {}, "stall_band": [0, 1]}
    for c in snap["classes"].values():
        c["criterion"] = ">= 2.0 min"
    rep = {"rule": C.RULE_VERSION, "floors": {"sessions": 5, "minutes": 2.0},
           "platforms": {"twitch": {"snapshot": snap, "deficits": [], "low_yield": [], "flags": [],
                                    "buffer_median_s_over_sessions": None, "twitch_latency_median_s_over_sessions": None}}}
    line = [x for x in C.markdown(rep) if x.startswith("schedule families, Q_1080p minutes")]
    assert line == ["schedule families, Q_1080p minutes (mean per tier session x tier sessions): TW-M 0.4 x 6, "
                    "TW-M@fast 1.25 x 3, TW-M@slow 0.0 x 2"], line


def test_the_record():
    sc = {**SH.schedule_of("TW-M@slow", 2, 23), "episodes": 3}
    sched = {k: v for k, v in sc.items() if k != "episode_s"}
    sh = session_meta.shaper_config("twitch", SH.TWITCH_TRANSITIONS_M, sc["episode_s"], matrix_id="TW-M@slow",
                                    low_state_cap=2, **sched)
    assert {k: sh[k] for k in ("matrix_id", "episode_s", "schedule", "low_cap_s", "episodes", "self_loops")} == {
        "matrix_id": "TW-M@slow", "episode_s": 460, "schedule": "slow", "low_cap_s": 120, "episodes": 3, "self_loops": False}
    assert CS.low_cap_s(sh) == 120 and CS.low_cap_s({"low_state_cap": 4, "episode_s": 60}) == 240
    src = (HERE / "exp.py").read_text()
    assert 'schedule={**SCHEDULE, "episodes": args.episodes,' in src and 'episode_time_in_seconds=SCHEDULE["episode_s"]' in src
    osrc = (HERE / "orchestrate.py").read_text()
    assert osrc.count("session_episodes(EPISODES_V2.get(p, 5), mid)") == 1   # run_batch (single mode: base length)
    # the trio refill (restart §4 / §2b): the drawn length and, @fast, the drawn episode length
    assert 'session_episodes(slen["minutes"], mid, fe["episode_s"] if fe else None)' in osrc


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_schedule_families: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
