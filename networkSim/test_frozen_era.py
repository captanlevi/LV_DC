#!/usr/bin/env python3
"""The frozen measure is era-gated (levi 00:51 known gap: "T*-era check_session re-runs at >= 6f4ce4d6 may include
picture runs; era-gated in the post-W-B batch"). A session whose meta.json concurrency.era is record.ERA (v3) reads
frozen_runs_all (stall-only + frozen-picture runs); any other era, and a session with no era (fail-safe: unknown = not
v3), reads addendum N's stall-only frozen_runs, so an old-era re-run reproduces its old classes. Pinned through every
reader: label_checks via labels_block (player_frozen, recovered_frozen), the per-re-entry read
(shared_deadline_reentries) and frozen_clock_of_record (held_frozen_s).

    python3 test_frozen_era.py        (exit 0 = pass)
"""
import csv
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as CS  # noqa: E402
import record  # noqa: E402

T0 = 1_000_000.0
COLS = ["ts", "type", "current_time", "total_frames", "paused", "is_ad"]


def _row(t, ty="res_meta", ct=None, tf=None):
    return {"ts": str(int(t * 1000)), "type": ty, "current_time": "" if ct is None else str(ct),
            "total_frames": "" if tf is None else str(tf), "paused": "False", "is_ad": "False"}


def _picture_only_rows(n=400, frozen=(50, 350)):
    """Labelled playing throughout (no stall event); the clock advances 1 s per tick; decoded frames advance except
    inside `frozen`, where total_frames stays flat: a frozen-PICTURE run of frozen[1] - frozen[0] s and no stall-only
    run."""
    rows, tf = [_row(T0, "video_start")], 0
    for i in range(n):
        if not frozen[0] < i <= frozen[1]:
            tf += 30
        rows.append(_row(T0 + i, ct=float(i), tf=tf))
    return rows


def _meta(era):
    return {"platform": "youtube", "concurrency": ({"mode": "trio", "record": True, "era": era} if era else
                                                   {"mode": "single"})}


ERAS = {"v3": record.ERA, "t58": record.T58_ERA, "t56_stated": "t56-nslot-2026-10-19", "unknown": None}


def test_era_predicate():
    assert record.ERA == "v3-collector-2026-10-24" and record.T58_ERA == "t58-restart-2026-10-21"
    assert CS.picture_runs_era(_meta(record.ERA))
    for era in (record.T58_ERA, "t56-nslot-2026-10-19", "t55-gro-off-2026-10-19", "", None):
        assert not CS.picture_runs_era(_meta(era)), era
    for m in (None, {}, {"concurrency": None}, {"concurrency": {"record": True}}):
        assert not CS.picture_runs_era(m), m                                 # fail-safe: unknown era = stall-only


def test_current_era_reads_the_union():
    """levi 9048534c: record.ERA (the era this collector writes) is one picture_runs_era treats as v3, so an ERA bump
    fails here unless check_session.PICTURE_RUN_ERAS is widened in the same commit."""
    assert record.ERA in CS.PICTURE_RUN_ERAS, (record.ERA, sorted(CS.PICTURE_RUN_ERAS))
    assert CS.picture_runs_era(_meta(record.ERA))
    assert record.T58_ERA not in CS.PICTURE_RUN_ERAS


def test_measure_selector():
    rows = _picture_only_rows()
    assert CS.frozen_runs(rows) == [] and CS.frozen_runs_all(rows) == [(T0 + 50, T0 + 350)]
    assert CS.frozen_runs_of(rows, False) == [] and CS.frozen_runs_of(rows, True) == [(T0 + 50, T0 + 350)]
    assert CS.frozen_playhead_run(rows) == 300.0 and CS.frozen_playhead_run(rows, picture=False) == 0.0


def _session(tmp: Path, era) -> Path:
    sd = tmp / "youtube" / f"yt_{era or 'none'}"
    sd.mkdir(parents=True)
    with open(sd / "labels.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(_picture_only_rows())
    (sd / "meta.json").write_text(json.dumps(_meta(era)))
    return sd


def test_labels_block_classes_by_era():
    """A picture-only frozen run of 300 s (> yt's T, 180 s): player_frozen on a v3-era session; on a t58 / stated /
    no-era session the measure is the stall-only one (0 s), the class the old era's check gave."""
    T = CS.FROZEN_PLAYHEAD_MAX_S["youtube"]
    assert T < 300.0
    with tempfile.TemporaryDirectory() as td:
        for name, era in ERAS.items():
            sd = _session(Path(td), era)
            _, _, lab = CS.labels_block(sd, json.loads((sd / "meta.json").read_text()), "youtube")
            want = 300.0 if name == "v3" else 0.0
            assert lab["frozen_playhead_max_s"] == want and lab["frozen_playhead_max_all_s"] == want, (name, lab)
            assert lab["frozen_playhead_class_s"] == want, (name, lab)
            assert CS.E.frozen_over_t(lab["frozen_playhead_class_s"], T) == (name == "v3"), name


def test_recovery_reads_the_same_measure():
    """recovered_frozen: a continue re-entry inside the picture run is a recovery only where that run is the measure."""
    rows, re_ = _picture_only_rows(), [{"outcome": "continue", "t_nav": T0 + 200}]
    assert CS.recovered_frozen(rows, re_)["recovered"]["recovered_frozen_s"] == 300.0
    assert CS.recovered_frozen(rows, re_, picture=False)["recovered"] is None
    lab_v3 = CS.label_checks(rows, "youtube", None, re_, picture_runs=True)
    lab_old = CS.label_checks(rows, "youtube", None, re_, picture_runs=False)
    assert lab_v3["recovered_frozen"] is not None and lab_old["recovered_frozen"] is None


def test_frozen_clock_of_record_by_era():
    """held_frozen_s reads the era's measure: a holding collector's yt session, one live net label (state 7, no hold)."""
    rows = _picture_only_rows()
    nl = [{"timestamp": T0, "speed": 900.0, "state": "7"}]
    held = sorted(CS.FROZEN_HOLD_COLLECTORS)[0]
    for name, era in ERAS.items():
        meta = {**_meta(era), "lvdc_commit": {"sha": held}}
        lab = CS.label_checks(rows, "youtube", None, [], picture_runs=CS.picture_runs_era(meta))
        CS.frozen_clock_of_record(lab, rows, "youtube", nl, meta)
        want = 300.0 if name == "v3" else 0.0
        assert lab["frozen_clock_of_record"]["held_s"] == want and lab["frozen_playhead_class_s"] == want, (name, lab)


def test_reentry_read_by_era():
    """shared_deadline_reentries (the per-re-entry read, tt) asks for the era's measure."""
    seen, real = [], CS.frozen_runs_of
    CS.frozen_runs_of = lambda rows, picture=True: seen.append(picture) or real(rows, picture)
    try:
        for era in ERAS.values():
            CS.shared_deadline_reentries({**_meta(era), "platform": "tiktok"}, _picture_only_rows(), [])
    finally:
        CS.frozen_runs_of = real
    assert seen == [True, False, False, False], seen


def main() -> int:
    test_era_predicate()
    test_current_era_reads_the_union()
    test_measure_selector()
    test_labels_block_classes_by_era()
    test_recovery_reads_the_same_measure()
    test_frozen_clock_of_record_by_era()
    test_reentry_read_by_era()
    print("test_frozen_era: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
