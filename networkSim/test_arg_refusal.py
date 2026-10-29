"""orchestrate's command-line refusals (hardening stop): one pure function, arg_refusal, every refusal exercised from a
valid record command line by one change, without starting a run; the record command itself is accepted, and the main
block calls the function before anything else."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orchestrate as O  # noqa: E402
O.network_core_installed = O.network_core_pin   # the record lines' venv is at the pin (test_nc_guard tests it)

RECORD = ["--batch", "youtube=99,twitch=99,tiktok=99", "--concurrent", "--trio-record", "--n-draw", "--window-min", "120",
          "--trio-yt-fullscreen", "--trio-layout", "composite", "--trio-steer", "--trio-steer-platforms", "youtube,twitch",
          "--expect-stratum", "steered"]                                       # collector v3 B5: a counted window states its stratum
import json  # noqa: E402
import tempfile  # noqa: E402

import data_root as DR  # noqa: E402

# t67 C8: a counted line needs the record root (RECORD_MARKER); every other concurrent line a dry root (none)
_REC, _DRY = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
(_REC / DR.RECORD_MARKER).write_text(json.dumps({"uuid": "TEST-VOL"}))
DR.volume_uuid = lambda p, run=None: "TEST-VOL"       # the volume under every tmp root, as findmnt would read it
ENV = {"LVDC_DATA_ROOT_UUID": "0000-test", "LVDC_DATA_ROOT": str(_REC)}


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
        (without(RECORD, "--window-min") + ["--window-min", "60"], "--trio-record needs --window-min >= 120"),
        (without(RECORD, "--window-min") + ["--window-min", "119.9"], "--trio-record needs --window-min >= 120"),
        (without(RECORD, "--trio-yt-fullscreen"), "--trio-record needs --trio-yt-fullscreen"),
        (RECORD + ["--max-per-slot", "1"], "--max-per-slot does not apply inside a window"),
        (without(RECORD, "--expect-stratum"), "--trio-record needs --expect-stratum"),                 # collector v3 B5
        (RECORD + ["--stratum-force", "lever"], "never with --trio-record"),
        (without(RECORD, "--trio-record") + ["--stratum-force", "lever"], "exclude each other"),
        (without(RECORD, "--trio-record", "--n-draw") + ["--trio-steer"], "--expect-stratum needs --concurrent and --n-draw"),
        (without(RECORD, "--trio-record", "--n-draw", "--expect-stratum", "--concurrent", "--window-min", "--trio-yt-fullscreen",
                 "--trio-layout", "--trio-steer", "--trio-steer-platforms") + ["--stratum-force", "lever"], "--stratum-force needs --concurrent"),
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
    ]
    for argv, want in cases:
        got = refusal(argv)
        assert got and want in got, (argv, got)
    assert "LVDC_DATA_ROOT_UUID" in refusal(RECORD, {}) and "LVDC_DATA_ROOT_UUID" in refusal(RECORD, {"LVDC_DATA_ROOT_UUID": " "})
    assert refusal(["--concurrent", "--platform", "youtube"]) == "--concurrent needs --batch"
    assert refusal(["--batch", "youtube=2,twitch=2,tiktok=1", "--concurrent", "--trio-stress", "--uplink-cap-kbit", "40000"],
                   {**ENV, "LVDC_DATA_ROOT": str(_DRY)}) is None                 # a stress arm on a dry root (C8)


def test_main_refuses_first():
    src = (HERE / "orchestrate.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    assert main.index("arg_refusal(args, os.environ)") < main.index("os.environ[\"LVDC_TRIO_DIR\"]") < main.index("run_trio(")


def main() -> int:
    test_record_line_accepted()
    test_each_refusal()
    test_main_refuses_first()
    test_c8_data_root_matches_the_run()      # audit B3: never run as a script
    print("test_arg_refusal: ok")
    return 0



def test_c8_data_root_matches_the_run():
    """t67 C8 (levi 18:45 / 18:56, brad): fail closed both ways, every mode. A counted run needs the marker and its
    volume's UUID (readable and equal); a run that is not of record is refused on any marked root, UUID readable or
    not; single mode never writes under a marked root."""
    dry = {**ENV, "LVDC_DATA_ROOT": str(_DRY)}
    nonrec = without(RECORD, "--trio-record", "--n-draw", "--window-min", "--expect-stratum")   # B5: a non-record line states none
    single = ["--batch", "youtube=1"]
    assert refusal(RECORD) is None and refusal(nonrec, dry) is None and refusal(single, dry) is None
    r = refusal(RECORD, dry)
    assert r and "not the verified record root" in r and "no marker" in r, r
    for argv in (nonrec, single):
        r = refusal(argv)
        assert r and "not of record on a marked record root" in r, (argv, r)
    saved = DR.volume_uuid
    try:
        DR.volume_uuid = lambda p, run=None: None                               # findmnt failing
        r = refusal(RECORD)
        assert r and "UUID unreadable" in r, r                                  # brad: a counted run refuses
        r = refusal(nonrec)
        assert r and "marked record root" in r, r                               # a dry run refuses any marker
        DR.volume_uuid = lambda p, run=None: "OTHER"                            # a copied marker
        assert "is not the volume's" in refusal(RECORD)
    finally:
        DR.volume_uuid = saved
    # brad / levi 19:00: exp.py's wall in every branch, by the nearest marked ancestor, before any dir is made
    assert DR.path_kind_refusal(_REC / "x", False) and "not of record under a marked" in DR.path_kind_refusal(_REC / "x", False)
    assert DR.path_kind_refusal(_REC / "_trio" / "deep" / "x", False)                       # --save-dir deep under it
    assert DR.path_kind_refusal(_REC / "_trio", True) is None                                # a counted slot run: allowed
    assert DR.path_kind_refusal(_DRY / "x", False) is None and "outside the record root" in DR.path_kind_refusal(_DRY / "x", True)
    saved = DR.volume_uuid
    try:
        DR.volume_uuid = lambda p, run=None: None
        assert "unverified" in DR.path_kind_refusal(_REC / "_trio", True)                    # findmnt failing: refused
    finally:
        DR.volume_uuid = saved
    src = (HERE / "exp.py").read_text()
    i = src.index("_c8 = _dr_c8.path_kind_refusal(_target, _of_record)")
    assert i < src.index("session_dir = args.save_dir", i) and i < src.index('session_dir = Path(__file__).parent.parent / "current_data"', i)
    assert '_of_record = bool(args.auto and SLOT is not None and os.environ.get("LVDC_TRIO_RECORD") == "1")' in src


if __name__ == "__main__":
    sys.exit(main())
