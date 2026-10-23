#!/usr/bin/env python3
"""The restart's random N per round (prereg §1, n_draw.py) and session length (prereg §4, shaping.draw_session_minutes):
the seeded draws, the fixed compositions and the N = 4 rotation, the round index from the slots log, the CLI guards,
the records (trio_up / meta.json concurrency.n_draw, shaper.session_length) and the episodes' scaling. No browser.

    python3 test_restart_n.py        (exit 0 = pass)
"""
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import n_draw as N  # noqa: E402
import record  # noqa: E402
from utils import shaping as S  # noqa: E402


def test_weights_and_compositions():
    assert N.weights_of((3, 4, 5)) == {3: 0.2, 4: 0.3, 5: 0.5}
    w = N.weights_of((3, 5))                                   # 4 failed its scaling gate: renormalised
    assert abs(w[3] - 0.2 / 0.7) < 1e-12 and abs(w[5] - 0.5 / 0.7) < 1e-12 and 4 not in w
    assert N.composition(3) == {"youtube": 1, "twitch": 1, "tiktok": 1}
    assert [N.composition(4, k) for k in range(4)] == [
        {"youtube": 2, "twitch": 1, "tiktok": 1}, {"youtube": 1, "twitch": 2, "tiktok": 1},
        {"youtube": 1, "twitch": 1, "tiktok": 2}, {"youtube": 2, "twitch": 1, "tiktok": 1}]
    # levi on brad's design check 00231: N = 5's single platform rotates yt -> tw -> tt (1/2/2, 2/1/2, 2/2/1)
    assert [N.composition(5, k) for k in range(4)] == [
        {"youtube": 1, "twitch": 2, "tiktok": 2}, {"youtube": 2, "twitch": 1, "tiktok": 2},
        {"youtube": 2, "twitch": 2, "tiktok": 1}, {"youtube": 1, "twitch": 2, "tiktok": 2}]
    assert all(sum(N.composition(n, 1).values()) == n for n in N.N_SUPPORT)
    # a composition failing its decode stress leaves its rotation; an N with none left leaves the support
    ex = N.parse_exclude("5:tiktok,4:youtube")
    assert ex == ((4, "youtube"), (5, "tiktok")) and N.rotation(5, ex) == ("youtube", "twitch")
    assert [N.composition(5, k, ex)["youtube"] for k in range(3)] == [1, 2, 1]
    assert [N.composition(4, k, ex) for k in range(2)] == [{"youtube": 1, "twitch": 2, "tiktok": 1},
                                                           {"youtube": 1, "twitch": 1, "tiktok": 2}]
    all5 = N.parse_exclude("5:youtube,5:twitch,5:tiktok")
    d = N.draw_at(0, {}, "e", True, exclude=all5)
    assert d["support"] == [3, 4, 5] and d["support_effective"] == [3, 4] and d["n"] in (3, 4)
    assert d["excluded"] == ["5:tiktok", "5:twitch", "5:youtube"]
    for bad in ("3:youtube", "5:x", "5"):
        try:
            N.parse_exclude(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert N.parse_support(None) == (3, 4, 5) and N.parse_support("5,3") == (3, 5)
    for bad in ("2,3", "x", "6"):
        try:
            N.parse_support(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_seeded_and_rotating():
    era = record.ERA
    a, b = N.simulate(300, era), N.simulate(300, era)
    assert a == b                                               # seeded: the same sequence every time
    ns = Counter(d["n"] for d in a)
    assert set(ns) == {3, 4, 5} and abs(ns[5] / 300 - 0.5) < 0.08 and abs(ns[4] / 300 - 0.3) < 0.08, ns
    for n, key in ((4, "doubled"), (5, "single")):
        ds = [d for d in a if d["n"] == n]
        assert [d["rotation"][key] for d in ds[:6]] == ["youtube", "twitch", "tiktok"] * 2      # yt -> tw -> tt
        assert [d["rotation"]["index"] for d in ds] == list(range(len(ds)))
    assert all(d["composition"] == N.composition(d["n"], (d.get("rotation") or {}).get("index", 0)) for d in a)
    per = Counter()                                             # brad's parity: no platform short by composition
    for d in a:
        per.update(d["composition"])
    assert (max(per.values()) - min(per.values())) / max(per.values()) <= 0.01, per     # the row: (max - min) / max
    pilot = N.simulate(300, era, record=False)
    assert [d["n"] for d in pilot] != [d["n"] for d in a]       # a pilot's sequence is its own
    other = N.simulate(300, "another-era")
    assert [d["n"] for d in other] != [d["n"] for d in a]
    assert all(d["n"] in (3, 5) for d in N.simulate(100, era, support=(3, 5)))


def test_round_index_from_the_slots_log():
    """The round's index and the N = 4 rotation come from the earlier trio_up draws of this era and record flag; a run
    that ends before its trio_up re-draws the same N; a pilot's or another era's draws never count."""
    era = record.ERA
    seq = N.simulate(12, era)
    with tempfile.TemporaryDirectory() as t:
        log = Path(t) / "trio_slots.jsonl"
        for bad in (log, None):                                 # levi 00138: a record draw fails closed
            try:
                N.draw(bad, era, True)
                raise AssertionError("no slots log: a record draw must fail closed")
            except RuntimeError:
                pass
        assert N.draw(log, era, False)["round"] == 0            # a pilot draw: lenient
        log.write_text("")                                      # an era's first record round: an empty log
        assert N.draw(log, era, True) == seq[0]                 # round 0
        assert N.draw(log, era, True) == seq[0]                 # nothing written: the same draw again
        with open(log, "w") as fh:
            fh.write(json.dumps({"event": "trio_up", "composition": {"youtube": 1}}) + "\n")      # an older run, no draw
            fh.write(json.dumps({"event": "trio_up", "n_draw": N.simulate(1, era, record=False)[0]}) + "\n")  # a pilot
            fh.write(json.dumps({"event": "trio_up", "n_draw": N.simulate(1, "t57-x")[0]}) + "\n")            # old era
            fh.write("not json\n")
            for d in seq[:7]:
                fh.write(json.dumps({"event": "trio_up", "n_draw": d}) + "\n")
                fh.write(json.dumps({"event": "start", "slot": "yt1", "n_draw": d}) + "\n")    # not a trio_up
        assert N.draw(log, era, True) == seq[7]
        assert N.draw(log, era, False)["round"] == 1            # the pilot's own sequence
        with open(log, "a") as fh:
            fh.write('{"event": "trio_up", "n_dr\n')            # a torn trio_up line: a record draw fails closed
        try:
            N.draw(log, era, True)
            raise AssertionError("an undecodable trio_up line must fail a record draw")
        except RuntimeError:
            pass
        assert N.draw(log, era, False)["round"] == 1


def test_a_void_run_is_not_a_draw_outside_the_t58_era():
    """levi 2026-10-23 18:58 (collector v3): a run that wrote trio_up but started no session (the w11 07:54 drain) is not a
    draw, so its relaunch redraws the same round with no operator step; the T* era (VOID_COUNTS_ERAS) keeps counting it."""
    era = "v3-test-era"
    seq = N.simulate(6, era)
    with tempfile.TemporaryDirectory() as t:
        log = Path(t) / "trio_slots.jsonl"
        with open(log, "w") as fh:
            for i, d in enumerate(seq[:2]):
                fh.write(json.dumps({"event": "trio_up", "run": f"r{i}", "n_draw": d}) + "\n")
                fh.write(json.dumps({"event": "start", "run": f"r{i}", "slot": "yt1"}) + "\n")
        assert N.draw(log, era, True) == seq[2]
        with open(log, "a") as fh:                              # the void run: trio_up + trio_down, no start
            fh.write(json.dumps({"event": "trio_up", "run": "void", "n_draw": seq[2]}) + "\n")
            fh.write(json.dumps({"event": "trio_down", "run": "void", "end_rc": 8}) + "\n")
        assert N.draw(log, era, True) == seq[2]                 # the relaunch redraws round 2
        with open(log, "a") as fh:
            fh.write(json.dumps({"event": "trio_up", "run": "w2", "n_draw": seq[2]}) + "\n")
            fh.write(json.dumps({"event": "start", "run": "w2", "slot": "tw1"}) + "\n")
        assert N.draw(log, era, True) == seq[3]
        t58 = record.ERA if record.ERA in N.VOID_COUNTS_ERAS else "t58-restart-2026-10-21"
        with open(log, "a") as fh:                              # the T* era: a void trio_up still counts (unchanged)
            fh.write(json.dumps({"event": "trio_up", "run": "v58", "n_draw": N.simulate(1, t58)[0]}) + "\n")
        assert N.draw(log, t58, True)["round"] == 1


def test_support_and_exclusions_frozen_per_era():
    """levi 00138: a record draw's support / exclusions are the era's last record draw's; a change needs a signed
    reference and may only narrow (an N out, a composition excluded), recorded in the draw."""
    era = record.ERA
    with tempfile.TemporaryDirectory() as t:
        log = Path(t) / "trio_slots.jsonl"
        log.write_text("")
        first = N.draw(log, era, True)
        assert first["support"] == [3, 4, 5] and first["excluded"] == [] and "change_sign" not in first
        log.write_text(json.dumps({"event": "trio_up", "n_draw": first}) + "\n")
        assert N.draw(log, era, True)["round"] == 1                                   # unchanged: fine
        for sup, exc in (((3, 5), ()), ((3, 4, 5), ((5, "tiktok"),))):              # unsigned changes: refused
            try:
                N.draw(log, era, True, sup, exclude=exc)
                raise AssertionError((sup, exc))
            except ValueError:
                pass
        d = N.draw(log, era, True, (3, 4, 5), exclude=((5, "tiktok"),), change_sign="levi 00140")
        assert d["excluded"] == ["5:tiktok"] and d["change_sign"] == "levi 00140"
        log.write_text(log.read_text() + json.dumps({"event": "trio_up", "n_draw": d}) + "\n")
        try:                                                                          # widening back: refused
            N.draw(log, era, True, (3, 4, 5), exclude=(), change_sign="levi 00141")
            raise AssertionError("widening must be refused")
        except ValueError:
            pass
        assert N.draw(log, era, True, (3, 4, 5), exclude=((5, "tiktok"),))["round"] == 2   # the new state holds
        assert N.draw(log, era, False, (3, 5))["round"] == 0                          # a pilot: never frozen


def test_cli_guards():
    def run(*a, env_add=None):
        env = {k: v for k, v in os.environ.items() if k != "LVDC_DATA_ROOT_UUID"}
        return subprocess.run([sys.executable, "orchestrate.py", "--batch", "youtube=1", *a], cwd=HERE,
                              capture_output=True, text=True, timeout=60, env={**env, **(env_add or {})})
    r = run("--concurrent", "--trio-record")
    assert r.returncode != 0 and "--trio-record needs --n-draw" in r.stderr, r.stderr
    for w in ((), ("--window-min", "90")):                       # levi 00138: the record's window is 120 min
        r = run("--concurrent", "--trio-record", "--n-draw", *w)
        assert r.returncode != 0 and "--trio-record needs --window-min 120" in r.stderr, r.stderr
    r = run("--concurrent", "--trio-record", "--n-draw", "--window-min", "120")     # levi on 2e666b0: §7 fullscreen
    assert r.returncode != 0 and "--trio-record needs --trio-yt-fullscreen" in r.stderr, r.stderr
    r = run("--concurrent", "--window-min", "120", "--max-per-slot", "1")
    assert r.returncode != 0 and "--max-per-slot does not apply inside a window" in r.stderr, r.stderr
    r = run("--concurrent", "--window-min", "0")
    assert r.returncode != 0 and "--window-min needs --concurrent and a positive length" in r.stderr, r.stderr
    r = run("--window-min", "30")
    assert r.returncode != 0 and "--window-min needs --concurrent" in r.stderr, r.stderr
    r = run("--concurrent", "--trio-record", "--n-draw", "--window-min", "120", "--trio-yt-fullscreen",
            "--session-range", "4,8")
    assert r.returncode != 0 and "--session-range is for an uncounted" in r.stderr, r.stderr
    for bad in ("4", "8,4", "x,y"):
        r = run("--concurrent", "--session-range", bad)
        assert r.returncode != 0 and "--session-range LO,HI" in r.stderr, (bad, r.stderr)
    # bundle item 6 (levi on a2f0cac): the record refuses an unset data volume UUID
    full = ("--concurrent", "--trio-record", "--n-draw", "--window-min", "120", "--trio-yt-fullscreen")
    r = run(*full)
    assert r.returncode != 0 and "--trio-record needs LVDC_DATA_ROOT_UUID" in r.stderr, r.stderr
    r = run(*full, env_add={"LVDC_DATA_ROOT_UUID": "  "})
    assert r.returncode != 0 and "--trio-record needs LVDC_DATA_ROOT_UUID" in r.stderr, r.stderr
    r = run("--concurrent", "--n-draw", "--slots", "youtube=2")
    assert r.returncode != 0 and "--n-draw needs --concurrent and no --slots" in r.stderr, r.stderr
    r = run("--n-draw")
    assert r.returncode != 0 and "--n-draw needs --concurrent" in r.stderr, r.stderr
    for a in (("--n-support", "3,5"), ("--n-exclude", "5:youtube"), ("--n-change-sign", "levi 1")):
        r = run("--concurrent", *a)
        assert r.returncode != 0 and "--n-support / --n-exclude / --n-change-sign need --n-draw" in r.stderr, r.stderr


def test_wiring():
    src = (HERE / "orchestrate.py").read_text()
    assert "nd = n_draw.draw(_RUNS_DIR / trio_slots.TRIO_DIR / trio_slots.SLOTS_LOG, record.ERA, TRIO_RECORD," in src
    assert 'layout=args.trio_layout, slots=comp, n_draw=nd, window_min=args.window_min))' in src
    assert '**({"n_draw": n_draw} if n_draw else {}),' in src                     # trio_up
    import trio_slots as T
    saved = os.environ.get("LVDC_N_DRAW")
    try:
        d = N.simulate(1, record.ERA)[0]
        os.environ["LVDC_N_DRAW"] = json.dumps(d)
        rec = T.concurrency_record(T.SLOTS["yt1"])
        assert rec["n_draw"] == d and rec["era"] == "t58-restart-2026-10-21"
        os.environ.pop("LVDC_N_DRAW")
        assert "n_draw" not in T.concurrency_record(T.SLOTS["yt1"])
    finally:
        if saved is not None:
            os.environ["LVDC_N_DRAW"] = saved


def test_session_length():
    a = S.draw_session_minutes("20261021_090000:yt1:1")
    assert a == S.draw_session_minutes("20261021_090000:yt1:1") and a["seed"] == 0 and a["rule"] == S.SESSION_LEN_RULE
    ms = [S.draw_session_minutes(f"R:tw1:{k}")["minutes"] for k in range(2000)]
    assert 10.0 <= min(ms) and max(ms) <= 40.0 and abs(sum(ms) / len(ms) - 25.0) < 0.8
    assert sum(m < 20 for m in ms) / len(ms) > 0.28 and sum(m > 30 for m in ms) / len(ms) > 0.28    # uniform, not centred
    # the episodes scale (Q am. 17): base 60 s episodes, @slow 3 of the session / 3, @fast 12 s
    assert S.session_episodes(17.4, "A") == 17 and S.session_episodes(17.4, "A@slow") == 3
    assert S.session_episodes(17.4, "A@fast") == 87
    assert S.schedule_of("A@slow", base_episodes=17.4)["episode_s"] == 17.4 * 60 / 3
    assert S.session_episodes(10.0, "TW-M") == 10 and S.session_episodes(40.0, "TT-M@fast") == 200
    src = (HERE / "orchestrate.py").read_text()
    assert 'slen = None if held else draw_session_minutes(f"{run_id}:{s.tag}:{started[tag] + 1}")' in src
    assert 'eps = EPISODES_V2.get(p, 5) if hold else session_episodes(slen["minutes"], mid, fe["episode_s"] if fe else None)' in src
    assert 'cmd += ["--session-minutes", str(slen["minutes"])]' in src
    exp = (HERE / "exp.py").read_text()
    assert ("SCHEDULE = schedule_of(matrix_id, low_cap, args.session_minutes or "
            "coverage_report.SESSION_EPISODES.get(platform),") in exp
    assert '**({"session_length": SESSION_LEN} if SESSION_LEN else {})},' in exp


def test_fast_episode_length():
    """Restart §2b: @fast's episode length per session, log-U(12, 48) s, seeded on the session-length key; the re-draw
    D/5-D/2; only @fast takes it."""
    import math
    a = S.draw_fast_episode_s("R:yt1:1")
    assert a == S.draw_fast_episode_s("R:yt1:1") and a["rule"] == S.FAST_EPISODE_RULE and a["seed"] == 0
    vs = [S.draw_fast_episode_s(f"R:tw1:{k}")["episode_s"] for k in range(3000)]
    assert 12.0 <= min(vs) and max(vs) <= 48.0
    lv = [math.log(v / 12) / math.log(4) for v in vs]          # log-uniform: uniform on the log scale
    assert abs(sum(lv) / len(lv) - 0.5) < 0.02 and abs(sum(x < 0.5 for x in lv) / len(lv) - 0.5) < 0.03
    assert abs(sum(v < 24 for v in vs) / len(vs) - 0.5) < 0.03  # the median is the geometric mean, 24 s
    sc = S.schedule_of("TT-M@fast", 4, episode_s=30.0)
    assert sc["episode_s"] == 30.0 and sc["redraw_s"] == [6.0, 15.0] and sc["low_cap_s"] == 240
    assert S.session_episodes(20.0, "TT-M@fast", 30.0) == 40 and S.session_episodes(20.0, "TT-M@fast") == 100
    for mid in ("TT-M@slow", "TT-M"):
        try:
            S.schedule_of(mid, 4, 20.0, episode_s=30.0)
            raise AssertionError(mid)
        except ValueError:
            pass
    exp = (HERE / "exp.py").read_text()
    assert "episode_s=args.fast_episode_s)" in exp and '"--fast-episode-s",' in exp


def test_abr_only_classes():
    """Restart §2b: tw Q_1080p, yt Q_240p, yt Q_480p are listed as unreachable ABR-only targets and never steered
    after; the report lists them with met / insufficient, never as a deficit."""
    import coverage_report as C
    from utils.shaping import PLATFORM_MATRICES
    assert set(C.ABR_ONLY) == {("youtube", "Q_240p"), ("youtube", "Q_480p")}      # tw Q_1080p: TW-H (prereg d32a48ce)
    cls = {c: {"sessions_ge_min": 5, "minutes": 50} for c in C.CLASSES["youtube"]}
    snap = {"platform": "youtube", "classes": {**cls, "Q_240p": {"sessions_ge_min": 0, "minutes": 0},
                                                "Q_480p": {"sessions_ge_min": 1, "minutes": 3}},
            "per_matrix": {}, "n_sessions": 5, "attempts": {"total": 5}}
    r = C.pick_matrix(snap, PLATFORM_MATRICES["youtube"], ["A"])
    assert r["target"] is None and r["default_rotation"], r                    # nothing steered after them
    assert set(r["unreachable_targets"]) >= {"Q_240p", "Q_480p"}
    assert sorted(r["abr_only"]["unreachable_abr_only"]) == ["Q_240p", "Q_480p"]
    assert C.abr_only(snap) == {"Q_240p": {"sessions_ge_min": 0, "floor": 5, "minutes": 0, "status": "insufficient"},
                                "Q_480p": {"sessions_ge_min": 1, "floor": 5, "minutes": 3, "status": "insufficient"}}
    tw = {"platform": "twitch", "classes": {c: {"sessions_ge_min": 5, "minutes": 50} for c in C.CLASSES["twitch"]}}
    assert C.abr_only(tw) == {}                    # tw Q_1080p is reachable through TW-H (prereg d32a48ce item 4)



def test_the_committed_simulation():
    """restart_simulation.py (for brad's design check): the committed fixture reproduces; its stagger is the trio's."""
    import hashlib
    import orchestrate as O
    import restart_simulation as R
    assert R.STAGGER_S == O.TRIO_STAGGER_S and R.WINDOW_MIN == O.RECORD_WINDOW_MIN == 120.0
    assert R.ALLOWANCE_S == O.WINDOW_ALLOWANCE_S == 180.0
    raw = R.FIXTURE.read_bytes()
    assert R.dumps(R.simulate()).encode() == raw, "the simulation no longer reproduces fixtures/restart_simulation.json"
    s = json.loads(raw)["summary"]
    assert s["windows"] == sum(s["n_counts"].values()) and s["rotations"]["4"][:3] == ["youtube", "twitch", "tiktok"]
    assert len(hashlib.sha256(raw).hexdigest()) == 64



def main() -> int:
    test_weights_and_compositions()
    test_seeded_and_rotating()
    test_round_index_from_the_slots_log()
    test_support_and_exclusions_frozen_per_era()
    test_cli_guards()
    test_wiring()
    test_session_length()
    test_fast_episode_length()
    test_abr_only_classes()
    test_the_committed_simulation()
    print("test_restart_n: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
