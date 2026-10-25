#!/usr/bin/env python3
"""v3 B4 x B5 (orchestrate's lever wiring): a lever window (drawn or forced) needs --lever-params, the frozen line's values
per platform (lever_params.load_params: all three platforms, exactly lambda_per_s / d_lo_s / d_hi_s, finite, lambda >= 0
(0: disabled),
0 < d_lo <= d_hi); its stratum record carries the params and their sha and turns steering off; every session gets
--lever <its platform's params>, a held decode_stress session never; --lever-params alone (no stratum flag) or an invalid
file is refused before trio_up, and so is a file whose sha256 differs from --lever-params-sha256 (required with it:
the dated prereg line's sha, levi 16:30 / 16:32); exp.py refuses --lever with --hold-state.

    python3 test_lever_wiring.py        (exit 0 = pass)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lever_params as L  # noqa: E402
import orchestrate as O  # noqa: E402
import stratum_draw as S  # noqa: E402

GOOD = {"youtube": {"lambda_per_s": 0.0097, "d_lo_s": 25.9, "d_hi_s": 55.9},
        "twitch": {"lambda_per_s": 0.008, "d_lo_s": 17.9, "d_hi_s": 47.9},
        "tiktok": {"lambda_per_s": 0.0039, "d_lo_s": 14.4, "d_hi_s": 44.4}}


def _file(d, obj):
    p = Path(d) / f"lp_{abs(hash(json.dumps(obj, sort_keys=True, default=str)))}.json"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return p


def test_load_params():
    with tempfile.TemporaryDirectory() as d:
        params, sha = L.load_params(_file(d, GOOD))
        assert params["twitch"] == {"lambda_per_s": 0.008, "d_lo_s": 17.9, "d_hi_s": 47.9} and len(sha) == 64
        off, _ = L.load_params(_file(d, {**GOOD, "tiktok": {**GOOD["tiktok"], "lambda_per_s": 0}}))   # levi 17:21
        assert off["tiktok"]["lambda_per_s"] == 0.0                                                    # lever disabled
        bad = [{k: v for k, v in GOOD.items() if k != "tiktok"},                                    # a platform missing
               {**GOOD, "youtube": {**GOOD["youtube"], "extra": 1}},                                # an extra key
               {**GOOD, "youtube": {**GOOD["youtube"], "d_lo_s": float("nan")}},                    # NaN
               {**GOOD, "youtube": {**GOOD["youtube"], "d_lo_s": True}},                            # a bool
               {**GOOD, "youtube": {**GOOD["youtube"], "lambda_per_s": -0.001}},                    # lambda < 0
               {**GOOD, "youtube": {**GOOD["youtube"], "d_lo_s": 60.0}},                            # d_lo > d_hi
               "not json", [1, 2]]
        for b in bad:
            try:
                L.load_params(_file(d, b))
                raise AssertionError(f"must refuse {b!r}")
            except ValueError:
                pass


def test_stratum_apply_lever():
    sd = {**S.draw_at(0, "e", True), "stratum": "lever"}
    rec, steer, ref = O.stratum_apply(sd, "lever", None, True)
    assert ref and "needs --lever-params" in ref
    lp = (GOOD, "a" * 64)
    rec, steer, ref = O.stratum_apply(sd, "lever", None, True, lp)
    assert ref is None and steer is False and rec["steering_off_by_stratum"] is True
    assert rec["lever_params"] == GOOD and rec["lever_params_sha256"] == "a" * 64 and rec["stratum"] == "lever"
    rec, steer, ref = O.stratum_apply(None, None, "lever", False, lp)                       # a dry calibration window
    assert ref is None and rec["forced"] is True and rec["lever_params"] == GOOD
    rec, steer, ref = O.stratum_apply({**sd, "stratum": "steered"}, "steered", None, True, lp)   # params unused elsewhere
    assert ref is None and steer is True and "lever_params" not in rec


def test_lever_cmd():
    saved = os.environ.get("LVDC_WINDOW_STRATUM")
    try:
        os.environ["LVDC_WINDOW_STRATUM"] = json.dumps({"stratum": "lever", "lever_params": GOOD})
        a = O.lever_cmd("twitch", None)
        assert a[0] == "--lever" and json.loads(a[1]) == GOOD["twitch"]
        assert O.lever_cmd("twitch", "3") == []                                             # a held session never levers
        os.environ["LVDC_WINDOW_STRATUM"] = json.dumps({"stratum": "lever", "lever_params": {}})
        assert O.lever_cmd("youtube", None) is None                                         # no values: refused upstream
        os.environ["LVDC_WINDOW_STRATUM"] = json.dumps({"stratum": "steered"})
        assert O.lever_cmd("youtube", None) == []
        os.environ.pop("LVDC_WINDOW_STRATUM")
        assert O.lever_cmd("youtube", None) == []
    finally:
        if saved is None:
            os.environ.pop("LVDC_WINDOW_STRATUM", None)
        else:
            os.environ["LVDC_WINDOW_STRATUM"] = saved
    src = (HERE / "orchestrate.py").read_text()
    assert "lever_args = lever_cmd(p, hold)" in src and "cmd += lever_args" in src


def test_arg_refusal():
    import data_root as DR
    dry_root = Path(tempfile.mkdtemp())
    denv = {"LVDC_DATA_ROOT_UUID": "0000-test", "LVDC_DATA_ROOT": str(dry_root)}
    base = ["--batch", "youtube=99,twitch=99,tiktok=99", "--concurrent", "--n-draw", "--window-min",
            str(O.RECORD_WINDOW_MIN), "--trio-yt-fullscreen", "--trio-layout", "composite"]
    r = lambda argv: O.arg_refusal(O.build_parser().parse_args(argv), denv)
    with tempfile.TemporaryDirectory() as d:
        good, bad = _file(d, GOOD), _file(d, {"youtube": GOOD["youtube"]})
        sha = L.load_params(good)[1]
        lp = lambda f, h=sha: ["--lever-params", str(f), "--lever-params-sha256", h]
        assert "only with --expect-stratum / --stratum-force" in r(base + lp(good))
        got = r(base + ["--stratum-force", "lever"] + lp(bad))
        assert got and got.startswith("--lever-params:"), got
        assert r(base + ["--stratum-force", "lever"] + lp(good)) is None
        assert "--lever-params" in r(base + ["--stratum-force", "lever"] + lp(Path(d) / "missing.json"))
        # levi 16:32: the pinned sha is required with the file and must match the loaded bytes
        assert "needs --lever-params-sha256" in r(base + ["--stratum-force", "lever", "--lever-params", str(good)])
        got = r(base + ["--stratum-force", "lever"] + lp(good, "0" * 64))
        assert got and "is not the pinned" in got, got
        assert r(base + ["--stratum-force", "lever"] + lp(good, sha.upper())) is None          # case-insensitive
        assert "only with --lever-params" in r(base + ["--stratum-force", "lever", "--lever-params-sha256", sha])
    assert DR is not None


def test_exp_refuses_lever_with_hold():
    src = (HERE / "exp.py").read_text()
    i = src.index('sys.exit("--lever and --hold-state exclude each other')
    assert i < src.index("    if args.hold_state is not None:      # t56 decode_stress")


def main() -> int:
    test_load_params()
    test_stratum_apply_lever()
    test_lever_cmd()
    test_arg_refusal()
    test_exp_refuses_lever_with_hold()
    print("test_lever_wiring: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
