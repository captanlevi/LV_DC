#!/usr/bin/env python3
"""levi 87562afd (#12, 2026-10-25): an ad-dominated qa_fail. check.json gains qa_fail_cause = "ad_dominated" when the
session is qa_fail, its only failing reason is the usable-share check, and the share would pass with its ad seconds
counted as usable; seconds inside a meta ad_wait.ad_stuck_run (an ad break stuck over a paused player, v3.5) count as
content in the share. A session without the record is computed as before (byte-identical check.json), and the
orchestrator's failure streak (round / window stop) skips an ad-dominated session.

    python3 test_qa_fail_cause.py        (exit 0 = pass)
"""
import contextlib
import csv
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as CS  # noqa: E402
import entries as E  # noqa: E402

T0 = 1_792_000_000.0
N = 1000
COLS = ["ts", "type", "video_w", "video_h", "current_time", "ad_signals", "src_changed", "paused", "ready_state"]
NET_OK = {"pcap": True, "packets": 10, "ssl_keys": True}   # a clean capture: only the labels decide the verdict


def _rows(ad=(100, 600), h0=(), n=N) -> list[dict]:
    """A yt session at 1 Hz: playing 720p, ad_signals on ticks in `ad` [lo, hi), video_h 0 (no stall) in `h0`."""
    rows = [{"ts": str((T0 - 1) * 1000), "type": "video_start", "video_w": "0", "video_h": "0", "current_time": "0",
             "ad_signals": "", "src_changed": "False", "paused": "False", "ready_state": "4"}]
    for k in range(n):
        in_ad = ad[0] <= k < ad[1]
        h = 0 if h0 and h0[0] <= k < h0[1] else 720
        rows.append({"ts": str((T0 + k) * 1000), "type": "res_meta", "video_w": "1280", "video_h": str(h),
                     "current_time": str(float(k)), "ad_signals": "yt_ad_showing" if in_ad else "",
                     "src_changed": "False", "paused": "False", "ready_state": "4"})
    return rows


def _classify(rows, runs=None, net=None) -> tuple[dict, str, list[str], str | None]:
    lab = CS.label_checks(rows, "youtube", ad_stuck_runs=runs)
    v, why = CS.verdict(lab, net or NET_OK, {}, [], CS.CHECKS["max_background_share"])
    cls = E.session_class("youtube", v, None, [], None, None, lab["frozen_playhead_max_s"], lab["frozen_playhead_T_s"])
    return lab, cls, why, CS.qa_fail_cause(cls, why, CS.usable_tally(rows, runs))


def test_ad_dominated_session_is_flagged() -> None:
    lab, cls, why, cause = _classify(_rows())
    assert lab["usable_share"] < CS.CHECKS["usable_share_min"], lab["usable_share"]
    assert cls == "qa_fail" and len(why) == 1 and why[0].startswith(CS.USABLE_REASON), (cls, why)
    assert cause == "ad_dominated"
    t = CS.usable_tally(_rows())
    assert t["usable"] / t["seconds"] == lab["usable_share"] and (t["usable"] + t["ad"]) / t["seconds"] >= 0.6, t


def test_ad_stuck_seconds_count_as_content() -> None:
    """The W-B yt_100003 case: most of the ad seconds are an ad break stuck over a paused, buffered player."""
    runs = [{"start": T0 + 100, "end": T0 + 560, "s": 460.0}]
    lab, cls, why, cause = _classify(_rows(), runs)
    assert lab["usable_share"] >= CS.CHECKS["usable_share_min"], lab["usable_share"]
    assert not any(r.startswith(CS.USABLE_REASON) for r in why) and cls == "normal" and cause is None, (cls, why)
    # a stuck run too short to lift the share: still the usable check, still ad-dominated (the remaining ads explain it)
    short = [{"start": T0 + 100, "end": T0 + 150, "s": 50.0}]
    lab2, cls2, why2, cause2 = _classify(_rows(), short)
    assert _classify(_rows())[0]["usable_share"] < lab2["usable_share"] < CS.CHECKS["usable_share_min"]
    assert cls2 == "qa_fail" and cause2 == "ad_dominated", (cls2, why2)
    # a stuck tick counts once: usable, never ad
    t = CS.usable_tally(_rows(), runs)
    assert t["usable"] + t["ad"] <= t["seconds"] and t["ad"] < CS.usable_tally(_rows())["ad"], t


def test_other_failing_check_is_not_flagged() -> None:
    _, cls, why, cause = _classify(_rows(), net={**NET_OK, "capture_gap_minutes": [3]})
    assert cls == "qa_fail" and len(why) == 2 and cause is None, (cls, why)
    # the usable check alone, but short of the bar even with the ads counted (h = 0 non-ad ticks): not ad-dominated
    rows = _rows(ad=(100, 400), h0=(400, 1000))
    _, cls, why, cause = _classify(rows)
    t = CS.usable_tally(rows)
    assert cls == "qa_fail" and len(why) == 1 and (t["usable"] + t["ad"]) / t["seconds"] < 0.6 and cause is None, t
    # not qa_fail (a higher class): never flagged
    assert CS.qa_fail_cause("player_frozen", why, CS.usable_tally(_rows())) is None
    assert CS.qa_fail_cause("qa_fail", why, None) is None


def test_old_session_without_ad_stuck_runs_unchanged() -> None:
    rows = _rows()
    *_, in_ad, stalled, h = CS._tick_states(rows)
    old = sum(1 for hh, s, ad in zip(h, stalled, in_ad) if not ad and (s or hh > 0))
    base = CS.label_checks(rows, "youtube")
    assert base["usable_share"] == old / CS.usable_tally(rows)["seconds"]          # the pre-#12 formula
    for meta in ({}, {"ad_wait": {"ad_runs": 3}}, {"ad_wait": {"ad_stuck_runs": []}}, {"ad_wait": None},
                 {"ad_wait": {"ad_stuck_runs": [{"start": None, "end": T0}]}}):
        runs = CS.ad_stuck_runs_of(meta)
        assert CS.label_checks(rows, "youtube", ad_stuck_runs=runs) == base, meta
    assert CS.ad_stuck_runs_of({"ad_wait": {"ad_stuck_runs": [{"start": 1, "end": 2}]}}) == [{"start": 1, "end": 2}]


def _session(tmp: str, rows: list[dict], meta: dict) -> Path:
    sd = Path(tmp) / "data" / "youtube" / "yt_99"
    sd.mkdir(parents=True)
    with open(sd / "labels.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, COLS)
        w.writeheader()
        w.writerows(rows)
    (sd / "meta.json").write_text(json.dumps({"platform": "youtube", "started_at": T0 - 5, "ended_at": T0 + N, **meta}))
    return sd


def _run_main(sd: Path) -> dict:
    """check_session.main on the session, its verdict read with a clean capture (labels decide, as in _classify)."""
    orig, argv = CS.verdict, sys.argv
    CS.verdict = lambda lab, net, shots, mm, bg, ab=None: orig(lab, NET_OK, {}, [], bg, ab)
    sys.argv = ["check_session.py", str(sd)]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            CS.main()
    finally:
        CS.verdict, sys.argv = orig, argv
    return json.loads((sd / "check.json").read_text())


def test_main_writes_the_flag() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        c = _run_main(_session(tmp, _rows(), {}))
        assert c["session_class"] == "qa_fail" and c["qa_fail_cause"] == "ad_dominated", (c["session_class"], c["reasons"])
        assert "qa_fail_cause ad_dominated" in (Path(c["session"]) / "check.md").read_text().splitlines()[0]
    with tempfile.TemporaryDirectory() as tmp:
        runs = [{"start": T0 + 100, "end": T0 + 560, "s": 460.0, "kind": "ad_stuck"}]
        c = _run_main(_session(tmp, _rows(), {"ad_wait": {"ad_stuck_runs": runs}}))
        assert c["session_class"] == "normal" and "qa_fail_cause" not in c, (c["session_class"], c["reasons"])
    with tempfile.TemporaryDirectory() as tmp:          # a check.json that is not ad-dominated has no key at all
        c = _run_main(_session(tmp, _rows(ad=(100, 120)), {}))
        assert c["session_class"] == "normal" and "qa_fail_cause" not in c


def test_orchestrator_streak_skips_ad_dominated() -> None:
    """No LV_DC path re-picks on qa_fail; the stop path is _account's failure streak (MAX_CONSECUTIVE_FAIL pauses the
    platform; two paused slots stop a window). An ad-dominated session leaves the streak as it was."""
    import orchestrate as O
    with tempfile.TemporaryDirectory() as d:
        troot = Path(d)
        sd = troot / "_failed" / "youtube" / "yt_100003"            # --move-failed archived it
        sd.mkdir(parents=True)
        (sd / "check.json").write_text(json.dumps({"verdict": "FAIL", "session_class": "qa_fail",
                                                   "qa_fail_cause": "ad_dominated"}))
        assert O._qa_fail_cause(troot, "youtube", "yt_100003") == "ad_dominated"
        assert O._qa_fail_cause(troot, "youtube", since=0.0) == "ad_dominated"
        assert O._qa_fail_cause(troot, "youtube", "yt_missing") is None and O._qa_fail_cause(troot, "youtube") is None
        k = O.MAX_CONSECUTIVE_FAIL - 1
        paused, fails, frozen, not_live = {}, {"yt1": k}, {"yt1": 0}, {}
        O._account("youtube", 0, 0, {"handle": "@c"}, None, 1.0, paused, fails, frozen, not_live, troot, key="yt1",
                   passed=False, qa_cause=O._qa_fail_cause(troot, "youtube", "yt_100003"))
        assert fails == {"yt1": k} and paused == {} and not_live == {}, (fails, paused, not_live)
        O._account("youtube", 0, 0, {"handle": "@c"}, None, 2.0, paused, fails, frozen, not_live, troot, key="yt1",
                   passed=False)                                       # any other qa_fail: as today
        assert fails == {"yt1": k + 1} and "yt" in paused.get("youtube", "") and not_live == {}, (fails, paused)
        paused, fails = {}, {"yt1": 2}
        O._account("youtube", 0, 0, None, None, 3.0, paused, fails, frozen, not_live, troot, key="yt1", passed=True,
                   qa_cause="ad_dominated")                            # a PASS still resets
        assert fails == {"yt1": 0}


def main() -> int:
    test_ad_dominated_session_is_flagged()
    test_ad_stuck_seconds_count_as_content()
    test_other_failing_check_is_not_flagged()
    test_old_session_without_ad_stuck_runs_unchanged()
    test_main_writes_the_flag()
    test_orchestrator_streak_skips_ad_dominated()
    print("test_qa_fail_cause: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
