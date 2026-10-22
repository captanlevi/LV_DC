"""orchestrate's command-line refusals (hardening stop): one pure function, arg_refusal, every refusal exercised from a
valid record command line by one change, without starting a run; the record command itself is accepted, and the main
block calls the function before anything else."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orchestrate as O  # noqa: E402

RECORD = ["--batch", "youtube=99,twitch=99,tiktok=99", "--concurrent", "--trio-record", "--n-draw", "--window-min", "120",
          "--trio-yt-fullscreen", "--trio-layout", "composite", "--trio-steer", "--trio-steer-platforms", "youtube,twitch"]
ENV = {"LVDC_DATA_ROOT_UUID": "0000-test"}


def refusal(argv, env=ENV):
    return O.arg_refusal(O.build_parser().parse_args(argv), env)


def without(argv, *flags):
    out, skip = [], 0
    for i, a in enumerate(argv):
        if skip:
            skip -= 1
            continue
        if a in flags:
            skip = 1 if i + 1 < len(argv) and not argv[i + 1].startswith("--") else 0
            continue
        out.append(a)
    return out


def test_record_line_accepted():
    assert refusal(RECORD) is None
    assert refusal(without(RECORD, "--trio-steer-platforms")) is None                  # every platform steered
    assert refusal(without(RECORD, "--trio-steer", "--trio-steer-platforms")) is None   # steering off


def test_each_refusal():
    cases = [
        (["--batch", "youtube=1", "--concurrent", "--trio-stress", "--trio-record"], "never --trio-record"),
        (RECORD + ["--trio-stress"], "never --trio-record"),
        (["--batch", "youtube=1", "--trio-stress"], "--trio-stress needs --concurrent"),
        (["--batch", "vimeo=1"], "unknown platform(s) in --batch"),
        (without(RECORD, "--concurrent"), "--trio-record needs --concurrent"),
        (without(RECORD, "--n-draw"), "needs --n-draw"),
        (RECORD + ["--slots", "yt,tw,tt"], "--n-draw needs --concurrent and no --slots"),
        (without(RECORD, "--n-draw", "--trio-record") + ["--n-support", "1"], "need --n-draw"),
        (without(RECORD, "--window-min") + ["--window-min", "0"], "positive length"),
        (without(RECORD, "--window-min") + ["--window-min", "60"], "--trio-record needs --window-min 120"),
        (without(RECORD, "--trio-yt-fullscreen"), "--trio-record needs --trio-yt-fullscreen"),
        (RECORD + ["--max-per-slot", "1"], "--max-per-slot does not apply inside a window"),
        (RECORD + ["--session-range", "10,20"], "never the record"),
        (without(RECORD, "--trio-record", "--n-draw", "--window-min") + ["--session-range", "x"], "LO,HI (minutes)"),
        (without(RECORD, "--trio-record", "--n-draw", "--window-min") + ["--session-range", "20,10"], "0 < LO <= HI"),
        (RECORD + ["--trio-pin", "yt1:state=1"], "--trio-pin needs --concurrent and --trio-pin-reason"),
        (RECORD + ["--trio-pin", "yt1:state=1", "--trio-pin-reason", "x"], "never with --trio-record"),
        (without(RECORD, "--trio-record") + ["--trio-pin", "yt1:nope=1", "--trio-pin-reason", "x"], "--trio-pin"),
        (without(RECORD, "--trio-record") + ["--trio-pin", "yt1:state=99", "--trio-pin-reason", "x"], "no shaper state"),
        (without(RECORD, "--trio-layout"), "--trio-yt-fullscreen needs --trio-layout composite"),
        (without(RECORD, "--trio-steer"), "--trio-steer-platforms needs --trio-steer"),
        (without(RECORD, "--trio-steer-platforms") + ["--trio-steer-platforms", ""], "unknown or empty"),
        (without(RECORD, "--trio-steer-platforms") + ["--trio-steer-platforms", " , "], "unknown or empty"),
        (without(RECORD, "--trio-steer-platforms") + ["--trio-steer-platforms", "youtube,vimeo"], "unknown or empty"),
        (RECORD + ["--uplink-cap-kbit", "40000"], "--uplink-cap-kbit needs --trio-stress"),
        (["--batch", "youtube=1", "--concurrent", "--trio-stress", "--uplink-cap-kbit", "0"], "positive rate"),
        # §9 trace regime (levi efb5e4cc)
        (RECORD + ["--trace-p", "0.1", "--link-capacity-kbit", "104100"], "positive --probe-goodput-kbit"),
        (RECORD + ["--trace-p", "0.1", "--probe-goodput-kbit", "90000"], "positive --probe-goodput-kbit"),
        (RECORD + ["--trace-p", "1", "--probe-goodput-kbit", "90000", "--link-capacity-kbit", "104100"],
         "needs --trio-stress"),
        (RECORD + ["--probe-goodput-kbit", "90000"], "--probe-goodput-kbit needs --trace-p"),
        (["--batch", "youtube=1", "--trace-p", "0.1"], "--trace-p needs --concurrent"),
    ]
    for argv, want in cases:
        got = refusal(argv)
        assert got and want in got, (argv, got)
    assert "LVDC_DATA_ROOT_UUID" in refusal(RECORD, {}) and "LVDC_DATA_ROOT_UUID" in refusal(RECORD, {"LVDC_DATA_ROOT_UUID": " "})
    assert refusal(["--concurrent", "--platform", "youtube"]) == "--concurrent needs --batch"
    assert refusal(["--batch", "youtube=2,twitch=2,tiktok=1", "--concurrent", "--trio-stress", "--uplink-cap-kbit", "40000"]) is None
    assert refusal(RECORD + ["--trace-p", "0.1", "--probe-goodput-kbit", "90000", "--link-capacity-kbit", "104100"]) is None
    assert refusal(["--batch", "youtube=1,twitch=1,tiktok=1", "--concurrent", "--trio-stress", "--trace-p", "1",
                    "--probe-goodput-kbit", "90000", "--link-capacity-kbit", "104100"]) is None     # G2: forced, stress


def test_main_refuses_first():
    src = (HERE / "orchestrate.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    assert main.index("arg_refusal(args, os.environ)") < main.index("os.environ[\"LVDC_TRIO_DIR\"]") < main.index("run_trio(")


def main() -> int:
    test_record_line_accepted()
    test_each_refusal()
    test_main_refuses_first()
    print("test_arg_refusal: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
