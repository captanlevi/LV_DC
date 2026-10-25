"""orchestrate.py --check-args (ops dry_window.sh, 2026-10-25): a plan line's parse and every refusal that needs no live
state, through the functions a real launch calls; one JSON line, exit 0 (ok) / 2 (refused), nothing written. Each
refused line here is also run as a real launch (no --check-args), which must exit with the same message before any live
step; those launches get PATH=/nonexistent and a temp dry data root, so nothing outside a temp dir can be reached."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import data_root as DR  # noqa: E402
import orchestrate as O  # noqa: E402

ORCH = str(HERE / "orchestrate.py")
# a dry gate line (C8: no marker under the data root), the record line's flags less the record ones
DRY = ["--batch", "youtube=99,twitch=99,tiktok=99", "--concurrent", "--n-draw", "--window-min", "120",
       "--trio-yt-fullscreen", "--trio-layout", "composite", "--trio-steer", "--stratum-force", "steered"]
RECORD = ["--batch", "youtube=99,twitch=99,tiktok=99", "--concurrent", "--trio-record", "--n-draw", "--window-min", "120",
          "--trio-yt-fullscreen", "--trio-layout", "composite", "--trio-steer", "--expect-stratum", "steered"]
# refused lines and the message: one argparse, one arg_refusal, one the main block's stratum_apply (not in arg_refusal)
REFUSED = [
    (DRY + ["--no-such-flag"], "orchestrate.py: error: unrecognized arguments: --no-such-flag"),
    ([a if a != "120" else "0" for a in DRY], "--window-min needs --concurrent and a positive length"),
    ([a for a in DRY if a != "--trio-steer"], "stratum steered needs --trio-steer; refused"),
    (DRY[:-1] + ["lever"], "stratum lever needs --lever-params (the frozen line's lever values); refused before trio_up"),
    (DRY[:-3] + ["--n-support", "3,9"], "--n-support '3,9': a non-empty subset of (3, 4, 5)"),   # no stratum, no steer
]


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _env(data_root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LVDC_")}
    return {**env, "LVDC_DATA_ROOT": str(data_root), "PATH": "/nonexistent", "PYTHONDONTWRITEBYTECODE": "1"}


def _run(argv, cwd: Path, data_root: Path):
    return subprocess.run([sys.executable, ORCH, *argv], cwd=cwd, env=_env(data_root), capture_output=True, text=True,
                          timeout=60)


def test_in_process():
    with tempfile.TemporaryDirectory() as dry, tempfile.TemporaryDirectory() as rec:
        env = {"LVDC_DATA_ROOT": dry}
        assert O.check_args(DRY, env) is None
        assert O.check_args(["--platform", "youtube", "--report-only"], env) is None
        for argv, want in REFUSED:
            assert O.check_args(argv, env) == want, (argv, O.check_args(argv, env))
        # arg_refusal is the same function: its message comes through unchanged
        assert O.check_args(DRY + ["--max-per-slot", "1"], env) == O.arg_refusal(O.build_parser().parse_args(
            DRY + ["--max-per-slot", "1"]), env)
        assert O.check_args(["--batch", "youtube=x"], env) == "ValueError: invalid literal for int() with base 10: 'x'"
        assert O.check_args([], env) == ("orchestrate.py: error: one of the arguments --batch --platform --all-platforms "
                                         "is required")
        # the record line on a verified record root passes (its stratum draw is left to the launch); C8 both ways
        (Path(rec) / DR.RECORD_MARKER).write_text(json.dumps({"uuid": "TEST-VOL"}))
        saved = DR.volume_uuid
        try:
            DR.volume_uuid = lambda p, run=None: "TEST-VOL"
            renv = {"LVDC_DATA_ROOT": rec, "LVDC_DATA_ROOT_UUID": "0000-test"}
            assert O.check_args(RECORD, renv) is None
            assert "not of record on a marked record root" in O.check_args(DRY, {"LVDC_DATA_ROOT": rec})
            assert "not the verified record root" in O.check_args(RECORD, {**renv, "LVDC_DATA_ROOT": dry})
        finally:
            DR.volume_uuid = saved


def test_cli_ok_no_side_effects():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as data:
        cwd, data = Path(cwd), Path(data)
        r = _run(["--check-args", *DRY], cwd, data)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert r.stdout.splitlines() == ['{"ok": true}'] and r.stderr == "", (r.stdout, r.stderr)
        assert _tree(cwd) == [] and _tree(data) == [], (_tree(cwd), _tree(data))


def test_cli_refusals_match_a_real_launch():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as data:
        cwd, data = Path(cwd), Path(data)
        for argv, want in REFUSED:
            r = _run(["--check-args", *argv], cwd, data)
            assert r.returncode == 2 and r.stderr == "", (argv, r.returncode, r.stderr)
            lines = r.stdout.splitlines()
            assert len(lines) == 1 and json.loads(lines[0]) == {"ok": False, "refusal": want}, (argv, r.stdout)
            real = _run(argv, cwd, data)                       # the real launch refuses at the same stage, same text
            assert real.returncode != 0 and real.stderr.strip().splitlines()[-1] == want, (argv, real.stderr)
        assert _tree(cwd) == [] and _tree(data) == [], (_tree(cwd), _tree(data))


def test_main_block_refusals_are_mirrored():
    """Drift guard: every sys.exit in the main block is either check_args' own, a refusal _static_refusal mirrors, one
    in CHECK_ARGS_NOT_COVERED, or a run's end. A new one fails here until it is placed."""
    src = (HERE / "orchestrate.py").read_text()
    main = src[src.index('if __name__ == "__main__":'):]
    exits = [ln.strip() for ln in main.splitlines() if "sys.exit(" in ln]
    assert exits == [
        "sys.exit(check_args_main(sys.argv[2:], os.environ))",
        "sys.exit(_refused)",                       # arg_refusal
        "sys.exit(_bad)",                           # coverage_allowed_parse
        "sys.exit(_pin_ref)",                       # pin_geometry_refusal
        "sys.exit(str(e))",                         # stratum_draw.draw: not covered (live)
        'sys.exit("--lever-params: the file changed after its check (sha mismatch); refused before trio_up")',  # not covered
        "sys.exit(_ref)",                           # stratum_apply
        "sys.exit(str(e))",                         # n_draw parse (covered) / n_draw.draw (not) / slots
        "sys.exit(run_trio(targets, yt_matrix=args.yt_matrix, rotate_geometry=not args.fixed_geometry,",
        "sys.exit(0)",
        "sys.exit(0)",
    ], exits
    st = src[src.index("def _static_refusal("):src.index("def check_args(")]
    for fn in ("arg_refusal(", "coverage_allowed_parse(", "batch_targets(", "parse_pin(", "pin_geometry_refusal(",
               "stratum_apply(", "parse_support(", "parse_exclude(", "parse_composition(", "build_slots("):
        assert fn in st and fn in main, fn


def main() -> int:
    test_in_process()
    test_cli_ok_no_side_effects()
    test_cli_refusals_match_a_real_launch()
    test_main_block_refusals_are_mirrored()
    print("test_check_args: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
