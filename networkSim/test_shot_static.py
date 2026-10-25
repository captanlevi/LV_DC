#!/usr/bin/env python3
"""levi 22:56 (WC2 yt_100001: the broadcaster sent a still for ~25 min while the player played normally): each shot row
carries its image's sha256; check_session's shots.static_run is a QA hint (never a verdict): the longest run of
consecutive cadence shots with one hash, hint at >= SHOTS_STATIC_MIN. Sessions without the hash keep check.json as before.

    python3 test_shot_static.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as C  # noqa: E402


def _index(d, rows):
    (Path(d) / "shots").mkdir(parents=True)
    (Path(d) / "shots" / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_a_static_run_is_a_hint():
    rows = [{"reason": "cadence", "t_decide": 100.0 + 60 * k, "sha256": "a" if k < 2 else "s"} for k in range(9)]
    rows.insert(4, {"reason": "tier_change", "t_decide": 290.0, "sha256": "x"})      # event shots never break a run
    with tempfile.TemporaryDirectory() as d:
        _index(d, rows)
        r = C.shots_static_run(Path(d))
    assert r["longest"] == 7 and r["hint"] is True and r["runs_ge_min"] == 1 and r["shots_in_runs_ge_min"] == 7
    assert r["from_ts"] == 220.0 and r["to_ts"] == 580.0 and r["cadence_shots"] == 9


def test_moving_content_is_no_hint_and_old_rows_are_untouched():
    with tempfile.TemporaryDirectory() as d:
        _index(d, [{"reason": "cadence", "t_decide": float(k), "sha256": str(k % 2)} for k in range(10)])
        r = C.shots_static_run(Path(d))
    assert r["longest"] == 1 and r["hint"] is False and r["runs_ge_min"] == 0
    with tempfile.TemporaryDirectory() as d:
        _index(d, [{"reason": "cadence", "t_decide": float(k)} for k in range(10)])     # an older build: no hash
        assert C.shots_static_run(Path(d)) is None
    assert C.shots_static_run(Path("/nonexistent")) is None
    with tempfile.TemporaryDirectory() as d:                                              # rows with ts only
        _index(d, [{"reason": "cadence", "ts": 10.0 * k, "sha256": "s"} for k in range(6)])
        r = C.shots_static_run(Path(d))
    assert r["hint"] and r["from_ts"] == 0.0 and r["to_ts"] == 50.0 and r["kind"] == "unknown"      # no rvfc rows
    assert "the image static" in r["rule"]


def _rvfc(d, rows):
    (Path(d) / "rvfc_frames.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_kind_splits_a_source_still_from_a_frozen_player():
    """levi 23:06: presented frames advancing over the run -> source_still; flat -> player_frozen (rvfc ts: page ms)."""
    shots = [{"reason": "cadence", "ts": 1000.0 + 60 * k, "sha256": "s"} for k in range(6)]
    for pf_step, want in ((30 * 60, "source_still"), (0, "player_frozen")):
        with tempfile.TemporaryDirectory() as d:
            _index(d, shots)
            _rvfc(d, [{"ts": (1000.0 + 60 * k) * 1000.0, "pf": 100 + pf_step * k, "el": 1} for k in range(6)])
            r = C.shots_static_run(Path(d))
        assert r["kind"] == want, (want, r)
    with tempfile.TemporaryDirectory() as d:                          # an element change: per element, summed
        _index(d, shots)
        _rvfc(d, [{"ts": 1000e3, "pf": 5, "el": 1}, {"ts": 1100e3, "pf": 3005, "el": 1},
                  {"ts": 1200e3, "pf": 2, "el": 2}, {"ts": 1300e3, "pf": 3002, "el": 2}])
        assert C.presented_rate(Path(d), 1000.0, 1300.0) == 20.0


def test_wiring():
    src = (HERE / "browser.py").read_text()
    assert '"sha256": hashlib.sha256(data).hexdigest(),' in src
    i = src.index("(shots_dir / name).write_bytes(data)")
    assert i < src.index('"sha256": hashlib.sha256(data).hexdigest(),')                # the bytes written, hashed
    cs = (HERE / "check_session.py").read_text()
    assert 'shots["static_run"] = _static' in cs and "static_run" not in cs[cs.index("def verdict("):cs.index("def verdict(") + 3000]


def main() -> int:
    test_a_static_run_is_a_hint()
    test_moving_content_is_no_hint_and_old_rows_are_untouched()
    test_the_kind_splits_a_source_still_from_a_frozen_player()
    test_wiring()
    print("test_shot_static: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
