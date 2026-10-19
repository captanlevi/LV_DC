#!/usr/bin/env python3
"""coverage_report's explore pick (t53 2026-10-19, levi + supervisor, for the N = 4 count): among the young matrices the
fewest record attempts (any outcome, running included), ties by a seeded draw (recorded), a matrix whose last
EXPLORE_REPEAT_CAP finished record sessions were all its own and none accepted passed over while another matrix is
eligible. At c1f9181 the sort-first young matrix was taken, so twitch's TW-L ran on every trio session while its failed
sessions never counted. Also: the trio's balance start state reads its own sessions of record (balance select).
Synthetic session dirs; no browser.

    python3 test_explore_pick.py        (exit 0 = pass)
"""
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import balance as B  # noqa: E402
import coverage_report as C  # noqa: E402
from test_coverage import session  # noqa: E402
from utils.shaping import PLATFORM_MATRICES  # noqa: E402

TW = {m: v for m, v in PLATFORM_MATRICES["twitch"].items() if "@" not in m}   # the base matrices (am. 17's
# schedule families: test_schedule_families.py)
ALL = [(c, 150) for c in C.CLASSES["twitch"] if c != "STALL"]


def _sess(root, name, matrix, t, accepted=True, failed=False, running=False, rec=True):
    """A twitch session of `matrix` started at t: accepted (PASS / normal), or a FAIL set aside in _failed, or running
    (meta.json only); rec: a session of record (select below)."""
    sd = session(root, "twitch", name, ALL, matrix=matrix, verdict="PASS" if accepted else "FAIL")
    meta = json.loads((sd / "meta.json").read_text())
    (sd / "meta.json").write_text(json.dumps({**meta, "started_at": t, "rec": rec}))
    if running:
        (sd / "check.json").unlink()
    if failed:
        dst = Path(root) / "_failed" / "twitch" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        sd.rename(dst)
        sd = dst
    return sd


def REC(meta):
    return bool(meta.get("rec"))


def test_the_c1f9181_case_moves_off_tw_l():
    """Four failed TW-L sessions of record and none accepted (counted round 1 at c1f9181): every matrix is young; TW-L
    has the most attempts and its last two finished sessions failed, so the pick is TW-M or current, by the seeded
    draw, recorded."""
    with tempfile.TemporaryDirectory() as d:
        for i in range(4):
            _sess(d, f"tw_{i}", "TW-L", 100.0 + i, accepted=False, failed=True)
        snap = C.snapshot(d, "twitch", REC)
        assert snap["per_matrix"] == {} and snap["attempts"] == {
            "per_matrix": {"TW-L": 4}, "total": 4, "recent": [["TW-L", False], ["TW-L", False]]}, snap["attempts"]
        r = C.pick_matrix(snap, TW)
        ex = r["explore"]
        # levi 2026-10-20: every deficit equal (no session yet): the target is the seeded draw among them, not class order
        tie = r["target_tie"]
        assert tie["seed"] == C.DEFICIT_TIE_SEED + 4 and tie["tied"][0] == "STALL" and "Q_1080p" not in tie["tied"], tie
        assert r["target"]["key"] == tie["drawn"] == random.Random(tie["seed"]).choice(tie["tied"]), r
        assert r["why"].startswith("explore"), r
        assert ex["young"] == ["TW-L", "TW-M", "current"] and ex["capped"] == ["TW-L"], ex
        assert ex["tied"] == ["TW-M", "current"] and ex["seed"] == C.EXPLORE_SEED + 4, ex
        assert r["matrix"] == random.Random(C.EXPLORE_SEED + 4).choice(["TW-M", "current"]) != "TW-L", r
        assert json.dumps(C.pick_matrix(C.snapshot(d, "twitch", REC), TW), sort_keys=True, default=str) == \
            json.dumps(r, sort_keys=True, default=str)                                         # reproducible


def test_fewest_attempts_any_outcome_running_included_non_record_ignored():
    """Attempts count accepted, failed and running sessions of record; a non-record session counts nowhere; only
    finished sessions enter `recent`."""
    with tempfile.TemporaryDirectory() as d:
        _sess(d, "a", "TW-L", 1.0, accepted=False, failed=True)
        _sess(d, "b", "TW-M", 2.0)                                     # accepted: TW-M has 1 < 2, still young
        _sess(d, "c", "current", 3.0, running=True)                    # running: an attempt, not finished
        _sess(d, "x", "current", 4.0, accepted=False, failed=True, rec=False)
        snap = C.snapshot(d, "twitch", REC)
        assert snap["attempts"] == {"per_matrix": {"TW-L": 1, "TW-M": 1, "current": 1}, "total": 3,
                                    "recent": [["TW-L", False], ["TW-M", True]]}, snap["attempts"]
        ex = C.pick_matrix(snap, TW)["explore"]
        assert ex["capped"] == [] and ex["tied"] == ["TW-L", "TW-M", "current"] and ex["seed"] == 3, ex
        _sess(d, "d", "TW-L", 5.0)                                     # TW-L now 2 attempts: the others are fewer
        ex = C.pick_matrix(C.snapshot(d, "twitch", REC), TW)["explore"]
        assert ex["tied"] == ["TW-M", "current"] and ex["matrix"] in ("TW-M", "current"), ex


def test_a_capped_last_young_matrix_yields_to_the_others_and_is_never_capped_alone():
    """Only TW-L young, its last two finished sessions failed: the explore passes, the STALL yields of the others decide
    (recorded); with no other allowed matrix the cap cannot apply (cap_overridden)."""
    with tempfile.TemporaryDirectory() as d:
        stall = [("STALL", 20), ("Q_480p", 5), ("STALL", 20)]
        for i, m in enumerate(("TW-M", "TW-M", "current", "current")):
            session(d, "twitch", f"ok{i}", ALL + (stall if m == "current" else []), matrix=m)
            p = Path(d) / "twitch" / f"ok{i}" / "meta.json"
            p.write_text(json.dumps({**json.loads(p.read_text()), "started_at": float(i), "rec": True}))
        for i in range(2):
            _sess(d, f"f{i}", "TW-L", 10.0 + i, accepted=False, failed=True)
        snap = C.snapshot(d, "twitch", REC)
        r = C.pick_matrix(snap, TW)
        assert r["explore"]["young"] == ["TW-L"] and r["explore"]["capped"] == ["TW-L"], r["explore"]
        assert r["explore"]["matrix"] is None and r["why"] == "mean stall onsets per session", r
        assert r["matrix"] == "current" and r["yields"] == {"TW-M": 0.0, "current": 2.0}, r
        alone = C.pick_matrix(snap, TW, ["TW-L"])
        assert alone["matrix"] == "TW-L" and alone["explore"]["cap_overridden"], alone
        _sess(d, "g", "TW-M", 20.0)                                    # another matrix in between: the cap lifts
        r = C.pick_matrix(C.snapshot(d, "twitch", REC), TW)
        assert r["matrix"] == "TW-L" and r["explore"]["capped"] == [], r


def test_the_balance_start_state_reads_only_the_selected_runs():
    """balance.load_platform_data / most_needed_state(select=): the trio's start state from its sessions of record
    (orchestrate passes the trio root and record.is_record)."""
    with tempfile.TemporaryDirectory() as d:
        for name, st, rec in (("r", "1", True), ("n", "9", False)):
            sd = Path(d) / "twitch" / name
            sd.mkdir(parents=True)
            (sd / "net_labels.txt").write_text("".join(json.dumps({"timestamp": 100.0 + 20 * k, "speed": 300, "state": st})
                                                       + "\n" for k in range(4)))
            (sd / "meta.json").write_text(json.dumps({"rec": rec}))
        dist_all, n_all = B.load_platform_data("twitch", Path(d))
        dist_rec, n_rec = B.load_platform_data("twitch", Path(d), REC)
        assert (n_all, n_rec) == (2, 1) and set(dist_rec) == {"1"} and set(dist_all) == {"1", "9"}, (dist_all, dist_rec)
    src = (Path(__file__).resolve().parent / "orchestrate.py").read_text()
    assert "most_needed_state(p, troot, yt_matrix=mid, matrix_id=mid, select=record.is_record)" in src


def test_one_membership_rule_for_the_trio_targets():
    """t58 L-3 / L-5 (levi; audit item 6): a counted run's targets and failure count count its sessions of record only
    (orchestrate.trio_select, record.is_record: the flag and the current ERA); a pilot run counts every trio session."""
    import orchestrate as O
    import record
    with tempfile.TemporaryDirectory() as d:
        # levi 2026-10-20: the record eras (the c1f9181 counted line's t55 and this era) count, the stated t56-nslot run
        # (61a852d rounds 1-3) never does
        for i, (rec, era) in enumerate(((True, record.ERA), (False, record.ERA), (True, "t56-nslot-2026-10-19"),
                                        (True, "t55-gro-off-2026-10-19"))):
            sd = _sess(d, f"tw_{i}", "TW-M", float(i))
            m = json.loads((sd / "meta.json").read_text())
            (sd / "meta.json").write_text(json.dumps({**m, "concurrency": {"record": rec, "era": era},
                                                      "lvdc_commit": {"sha": "c1f9181"}}))
        saved = (O.TRIO_RECORD, O.TRIO_STRESS, O.TRIO_RUN_ID)
        try:
            O.TRIO_RECORD = True
            assert O.trio_select() is record.is_record and O.accepted_sessions("twitch", Path(d), O.trio_select()) == 2
            O.TRIO_RECORD = False
            assert O.trio_select() is None and O.accepted_sessions("twitch", Path(d), O.trio_select()) == 4
            # decode_stress (shagun 2026-10-19): its targets count this run's sessions only, never an earlier run's
            m = json.loads((Path(d) / "twitch" / "tw_1" / "meta.json").read_text())
            (Path(d) / "twitch" / "tw_1" / "meta.json").write_text(json.dumps(
                {**m, "concurrency": {**m["concurrency"], "trio_run": "R2"}}))
            O.TRIO_STRESS, O.TRIO_RUN_ID = True, "R2"
            assert O.accepted_sessions("twitch", Path(d), O.trio_select()) == 1
            O.TRIO_RUN_ID = "R3"
            assert O.accepted_sessions("twitch", Path(d), O.trio_select()) == 0
        finally:
            O.TRIO_RECORD, O.TRIO_STRESS, O.TRIO_RUN_ID = saved
    assert record.ERA == "t57-n5-boundary-2026-10-20"
    assert record.RECORD_ERAS == ("t55-gro-off-2026-10-19", record.ERA) and record.STATED_ERAS == ("t56-nslot-2026-10-19",)
    stated = {"concurrency": {"record": True, "era": "t56-nslot-2026-10-19"}}
    assert not record.is_record(stated) and not record.of_record_any_era(stated)          # never, not even not_usable
    assert record.of_record_any_era({"concurrency": {"record": True, "era": "t55-gro-off-2026-10-19"}})
    # levi on 844f115: t55 counts on its counted line only (c1f9181), never e62883d / 9dc66cb's earlier rounds
    t55 = lambda sha: {"concurrency": {"record": True, "era": "t55-gro-off-2026-10-19"}, "lvdc_commit": {"sha": sha}}
    assert record.is_record(t55("c1f9181" + "0" * 33)) and not record.is_record(t55("e62883d" + "0" * 33))
    assert not record.is_record(t55("9dc66cb")) and not record.is_record(t55(None))
    assert record.is_record({"concurrency": {"record": True, "era": record.ERA}, "lvdc_commit": {"sha": "844f115"}})
    assert record.RECORD_LINES == {"t55-gro-off-2026-10-19": ("c1f9181",), record.ERA: ()}


def test_failure_streaks_are_per_slot_and_flags_per_session():
    """t58 N-4 (brad's audit): with two slots of one platform, a slot's consecutive failures are its own (a sibling's
    PASS no longer resets them) and judged from its own session (the slots log's start for the slot in this run); a
    slot's exit reports flags for its own session only."""
    import orchestrate as O
    with tempfile.TemporaryDirectory() as d:
        troot = Path(d)
        log = troot / "_summaries" / "trio_slots.jsonl"
        log.parent.mkdir(parents=True)
        ok = _sess(d, "tw_100001", "TW-M", 1.0)                                   # tw1's session: PASS
        bad = _sess(d, "tw_100002", "TW-M", 2.0, accepted=False, failed=True)     # tw2's session: FAIL
        log.write_text("".join(json.dumps(e) + "\n" for e in (
            {"event": "start", "slot": "tw1", "session": "tw_100001", "platform": "twitch", "run": "R"},
            {"event": "start", "slot": "tw2", "session": "tw_100002", "platform": "twitch", "run": "R"},
            {"event": "start", "slot": "tw2", "session": "tw_old", "platform": "twitch", "run": "OLD"})))
        assert O._slot_session(log, "tw2", "R") == "tw_100002" and O._slot_session(log, "tw9", "R") is None
        assert O._session_passed(troot, "twitch", "tw_100001") and not O._session_passed(troot, "twitch", "tw_100002")
        paused, fails, frozen = {}, {"tw1": 0, "tw2": O.MAX_CONSECUTIVE_FAIL - 1}, {"tw1": 0, "tw2": 0}
        # the old delta rule would read tw1's PASS as tw2's success (accepted count went up): now tw2's own FAIL counts
        O._account("twitch", 1, 0, None, None, 0.0, paused, fails, frozen, {}, troot, key="tw2",
                   passed=O._session_passed(troot, "twitch", O._slot_session(log, "tw2", "R")))
        assert fails == {"tw1": 0, "tw2": O.MAX_CONSECUTIVE_FAIL} and "slot tw2" in paused.get("twitch", ""), (fails, paused)
        for sd, flag in ((ok, "unexpected_codec"), (bad, "single_rung_ladder")):
            c = json.loads((sd / "check.json").read_text())
            (sd / "check.json").write_text(json.dumps({**c, "suspicious": [{"flag": flag}]}))
        rows = O.report_flags("twitch", 0, troot, only="tw_100002")
        assert [r["session"] for r in rows] == ["tw_100002"], rows


def test_a_pinned_fullscreen_waits_for_the_token():
    """t58 N-6: a pin's fullscreen geometry goes through the one-fullscreen token: deferred (drawn geometry kept,
    recorded) while a sibling holds fullscreen; applied otherwise; a non-fullscreen pin applies as before."""
    import orchestrate as O
    pin = {"geometry": "fullscreen:1920x1080", "reason": "r"}
    held = {"yt2": {"geom": "fullscreen:1920x1080"}, "tw1": {"geom": "window:1920x1080"}}
    assert O.pin_geometry(pin, "player:853x480", held, "yt1") == ("player:853x480",
                                                                  {**pin, "geometry_deferred": "fullscreen:1920x1080"})
    assert O.pin_geometry(pin, "player:853x480", {"yt1": {"geom": "fullscreen:1920x1080"}}, "yt1") == \
        ("fullscreen:1920x1080", pin)                                   # its own (previous) session holds nothing now
    assert O.pin_geometry({"geometry": "window:1920x1080"}, "player:853x480", held, "tw2")[0] == "window:1920x1080"
    assert O.pin_geometry({"state": "3"}, "player:853x480", held, "tw2") == ("player:853x480", {"state": "3"})


def test_trio_stress_sessions_never_count_as_single_mode():
    """t58 N-9: single mode's history readers skip every trio-family root (_trio, _trio_stress), not only the exact
    "_trio" (geometry / tw_categories / yt_channels collected_counts, yt last_channel)."""
    import geometry as G
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        spec = G.GEOMETRY_CHOICES["youtube"][0]
        mode, size = spec.split(":", 1)
        for rel in ("youtube/yt_1", "_trio/youtube/yt_100001", "_trio_stress/youtube/yt_100002"):
            (d / rel).mkdir(parents=True)
            (d / rel / "meta.json").write_text(json.dumps({"geometry_target": spec}))
        assert G.collected_counts(d, "youtube")[spec] == 1, G.collected_counts(d, "youtube")
    src = "".join((Path(__file__).resolve().parent / f).read_text() for f in ("yt_channels.py", "tw_categories.py"))
    assert '"_trio" in ' not in src and '"_trio" not in ' not in src


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_explore_pick: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
