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

TW = PLATFORM_MATRICES["twitch"]
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
        assert r["target"]["key"] == "STALL" and r["why"].startswith("explore"), r
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


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_explore_pick: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
