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
    assert N.composition(5) == {"youtube": 2, "twitch": 2, "tiktok": 1}
    assert [N.composition(4, k) for k in range(4)] == [
        {"youtube": 2, "twitch": 1, "tiktok": 1}, {"youtube": 1, "twitch": 2, "tiktok": 1},
        {"youtube": 1, "twitch": 1, "tiktok": 2}, {"youtube": 2, "twitch": 1, "tiktok": 1}]
    assert all(sum(N.composition(n, 1).values()) == n for n in N.N_SUPPORT)
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
    fours = [d for d in a if d["n"] == 4]
    assert [d["doubled"] for d in fours[:6]] == ["youtube", "twitch", "tiktok"] * 2      # yt -> tw -> tt
    assert [d["n4_index"] for d in fours] == list(range(len(fours)))
    assert all(d["composition"] == N.composition(d["n"], d.get("n4_index", 0)) for d in a)
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
        assert N.draw(log, era, True) == seq[0]                 # no log yet: round 0
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


def test_cli_guards():
    def run(*a):
        return subprocess.run([sys.executable, "orchestrate.py", "--batch", "youtube=1", *a], cwd=HERE,
                              capture_output=True, text=True, timeout=60)
    r = run("--concurrent", "--trio-record")
    assert r.returncode != 0 and "--trio-record needs --n-draw" in r.stderr, r.stderr
    r = run("--concurrent", "--n-draw", "--slots", "youtube=2")
    assert r.returncode != 0 and "--n-draw needs --concurrent and no --slots" in r.stderr, r.stderr
    r = run("--n-draw")
    assert r.returncode != 0 and "--n-draw needs --concurrent" in r.stderr, r.stderr
    r = run("--concurrent", "--n-support", "3,5")
    assert r.returncode != 0 and "--n-support needs --n-draw" in r.stderr, r.stderr


def test_wiring():
    src = (HERE / "orchestrate.py").read_text()
    assert "nd = n_draw.draw(_RUNS_DIR / trio_slots.TRIO_DIR / trio_slots.SLOTS_LOG, record.ERA, TRIO_RECORD," in src
    assert 'layout=args.trio_layout, slots=comp, n_draw=nd))' in src
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
    assert 'slen = None if hold else draw_session_minutes(f"{run_id}:{s.tag}:{started[tag] + 1}")' in src
    assert 'eps = EPISODES_V2.get(p, 5) if hold else session_episodes(slen["minutes"], mid)' in src
    assert 'cmd += ["--session-minutes", str(slen["minutes"])]' in src
    exp = (HERE / "exp.py").read_text()
    assert ("SCHEDULE = schedule_of(matrix_id, low_cap, args.session_minutes or "
            "coverage_report.SESSION_EPISODES.get(platform))") in exp
    assert '**({"session_length": SESSION_LEN} if SESSION_LEN else {})},' in exp


def main() -> int:
    test_weights_and_compositions()
    test_seeded_and_rotating()
    test_round_index_from_the_slots_log()
    test_cli_guards()
    test_wiring()
    test_session_length()
    print("test_restart_n: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
