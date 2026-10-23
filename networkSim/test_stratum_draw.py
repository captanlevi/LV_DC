"""Collector v3 B5 (t69 interface 8b48e25a; levi / peter 2026-10-23 18:02-18:03): the per-window stratum, in permuted blocks of 4
(2 steered, 1 unsteered, 1 lever), keyed by n_draw's round index so a void window keeps its stratum; one function that
rounds_ref_n and orchestrate both compute; orchestrate refuses a mismatch, a steered window without --trio-steer and any lever
window (B4's arm not in this collector), turns steering off for an unsteered one, and records the stratum in trio_up and every
session's meta.concurrency.window_stratum.

    python3 test_stratum_draw.py        (exit 0 = pass)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orchestrate as O  # noqa: E402
import stratum_draw as S  # noqa: E402

ERA = "v3-test-era"


def test_every_block_holds_two_steered_one_unsteered_one_lever():
    seq = [S.draw_at(k, ERA, True)["stratum"] for k in range(400)]
    for b in range(100):
        assert Counter(seq[4 * b:4 * b + 4]) == Counter({"steered": 2, "unsteered": 1, "lever": 1}), b
    assert len({tuple(S.block_order(ERA, b)) for b in range(100)}) > 6        # the order varies across blocks


def test_reproducible_from_era_and_k_alone():
    a = [S.draw_at(k, ERA, True) for k in range(12)]
    assert a == [S.draw_at(k, ERA, True) for k in range(12)]
    assert [d["block"] for d in a[:8]] == [0, 0, 0, 0, 1, 1, 1, 1] and [d["pos"] for d in a[:5]] == [0, 1, 2, 3, 0]
    assert any(S.block_order(ERA, b) != S.block_order("other-era", b) for b in range(8))
    assert S.block_order(ERA, 0) == S.block_order(ERA, 0)                     # sha256-defined, no process state


def _log(path: Path, draws: list[tuple[str, bool]]):
    with open(path, "w") as f:
        for i, (era, rec) in enumerate(draws):
            f.write(json.dumps({"event": "trio_up", "run": f"r{i}", "n_draw": {"era": era, "record": rec, "n": 3}}) + "\n")
            f.write(json.dumps({"event": "start", "run": f"r{i}", "slot": "yt1"}) + "\n")


def test_k_is_n_draws_round_index_and_a_pilot_never_moves_the_record_sequence():
    with tempfile.TemporaryDirectory() as t:
        log = Path(t) / "trio_slots.jsonl"
        _log(log, [(ERA, True), (ERA, False), (ERA, True), ("older-era", True), (ERA, True)])
        d = S.draw(log, ERA, True)
        assert d["k"] == 3 and d == S.draw_at(3, ERA, True)                # 3 earlier record draws of this era
        assert S.draw(log, ERA, False)["k"] == 1                           # the pilot sequence is its own
        log.write_text("")
        assert S.draw(log, ERA, True)["k"] == 0                            # an era's first window
        try:
            S.draw(Path(t) / "missing.jsonl", ERA, True)
            raise AssertionError("an unreadable log must fail closed for a record draw")
        except RuntimeError:
            pass


def test_stratum_apply_refusals_and_steering():
    sd = lambda s: {**S.draw_at(0, ERA, True), "stratum": s}
    assert O.stratum_apply(None, None, None, True) == (None, True, None)       # no stratum flags: nothing changes
    rec, steer, ref = O.stratum_apply(sd("steered"), "unsteered", None, True)
    assert ref and "mismatch" in ref
    assert O.stratum_apply(sd("steered"), "steered", None, True)[1:] == (True, None)
    assert "needs --trio-steer" in O.stratum_apply(sd("steered"), "steered", None, False)[2]
    rec, steer, ref = O.stratum_apply(sd("unsteered"), "unsteered", None, True)
    assert ref is None and steer is False and rec["steering_off_by_stratum"] is True
    assert "lever" in O.stratum_apply(sd("lever"), "lever", None, True)[2]
    rec, steer, ref = O.stratum_apply(sd("lever"), None, "unsteered", False)    # a dry run's stated stratum
    assert ref is None and rec["stratum"] == "unsteered" and rec["forced"] is True


def test_arg_refusals():
    base = ["--batch", "youtube=99,twitch=99,tiktok=99", "--concurrent", "--trio-record", "--n-draw", "--window-min",
            str(O.RECORD_WINDOW_MIN), "--trio-yt-fullscreen", "--trio-layout", "composite", "--trio-steer"]
    env = {"LVDC_DATA_ROOT_UUID": "0000-test"}
    r = lambda argv: O.arg_refusal(O.build_parser().parse_args(argv), env)
    assert r(base + ["--expect-stratum", "steered"]) is None
    assert "--expect-stratum" in r(base)                                       # a counted window states its stratum
    assert "dry / gate run only" in r(base + ["--expect-stratum", "steered", "--stratum-force", "lever"])
    dry = [a for a in base if a != "--trio-record"]
    assert r(dry + ["--stratum-force", "unsteered"]) is None
    assert "exclude each other" in r(dry + ["--stratum-force", "unsteered", "--expect-stratum", "steered"])
    nodraw = [a for a in dry if a != "--n-draw"]
    assert "--n-draw" in r(nodraw + ["--stratum-force", "steered"])


def test_the_stratum_reaches_every_sessions_meta():
    import trio_slots as T
    rec = S.draw_at(5, ERA, True)
    saved = os.environ.get("LVDC_WINDOW_STRATUM")
    try:
        os.environ["LVDC_WINDOW_STRATUM"] = json.dumps(rec)
        slot = next(iter(T.build_slots({"youtube": 1, "twitch": 1, "tiktok": 1}).values()))
        assert T.concurrency_record(slot)["window_stratum"] == rec
        os.environ.pop("LVDC_WINDOW_STRATUM")
        assert "window_stratum" not in T.concurrency_record(slot)
    finally:
        if saved is not None:
            os.environ["LVDC_WINDOW_STRATUM"] = saved


def main() -> int:
    for name, f in sorted(globals().items()):
        if name.startswith("test_") and callable(f):
            f()
    print("test_stratum_draw: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
