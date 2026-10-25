#!/usr/bin/env python3
"""levi 02:12 (brad 02:11; chaos's shared collector venv found at network_core 0.6.7 with setup.sh pinning 0.8.1): a
record run refuses before trio_up unless the installed network_core is exactly setup.sh's pin (one source), in the real
launch and --check-args alike (arg_refusal); setup.sh's install is fatal on a record box.

    python3 test_nc_guard.py        (exit 0 = pass)
"""
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import orchestrate as O  # noqa: E402


def test_the_pin_is_read_from_setup_sh():
    assert O.network_core_pin() == "0.8.2"                     # nc 0.8.2 (levi 08:51; published 09:15)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "s.sh").write_text('pip install --quiet "network_core>=0.5.3"\n')
        assert O.network_core_pin(Path(d) / "s.sh") is None
        (Path(d) / "s.sh").write_text('X=1\nNC_PIN=0.9.0\npip install "network_core==${NC_PIN}"\n')
        assert O.network_core_pin(Path(d) / "s.sh") == "0.9.0"
        (Path(d) / "s.sh").write_text('# NC_PIN=0.9.0 (a comment, not the pin)\n')
        assert O.network_core_pin(Path(d) / "s.sh") is None


def test_the_refusal():
    pin = lambda: "0.8.1"                                                   # noqa: E731
    assert O.network_core_refusal(pin, lambda: "0.8.1") is None
    r = O.network_core_refusal(pin, lambda: "0.6.7")
    assert r and "network_core 0.6.7" in r and "pins 0.8.1" in r and "refused before trio_up" in r
    assert "not installed" in O.network_core_refusal(pin, lambda: None)
    assert "pins no network_core version" in O.network_core_refusal(lambda: None, lambda: "0.8.1")


def test_record_lines_are_refused_off_the_pin_and_dry_lines_never():
    """arg_refusal (the launch and --check-args): --trio-record off the pin is refused; a dry line never reads it."""
    import test_check_args as TC
    import data_root as DR
    saved, saved_kind = O.network_core_installed, DR.kind_refusal
    try:
        DR.kind_refusal = lambda record, root: None       # C8 passes: the version check runs last, after it
        O.network_core_installed = lambda: "0.6.7"
        args = O.build_parser().parse_args(TC.RECORD)
        r = O.arg_refusal(args, {"LVDC_DATA_ROOT_UUID": "x"})
        assert r and "network_core 0.6.7" in r, r
        O.network_core_installed = O.network_core_pin
        assert O.arg_refusal(args, {"LVDC_DATA_ROOT_UUID": "x"}) is None
        O.network_core_installed = lambda: "0.6.7"
        assert "network_core" not in str(O.arg_refusal(O.build_parser().parse_args(TC.DRY), {}))
        # every argument refusal comes first: a malformed record line names its own defect, not the version
        assert "LVDC_DATA_ROOT_UUID" in O.arg_refusal(args, {})
    finally:
        O.network_core_installed, DR.kind_refusal = saved, saved_kind


def test_setup_sh_is_fatal_on_a_record_box():
    src = (HERE.parent / "setup.sh").read_text()
    assert src.count("0.8.2") == 1 and src.count('network_core==${NC_PIN}') == 2   # brad 02:51: one literal, two uses
    blk = src[src.index("\nNC_PIN=0.8.2\n"):][:700]
    assert 'LVDC_RECORD_BOX:-0}" = 1' in blk and ".lvdc_record_root" in blk and "exit 1" in blk
    assert subprocess.run(["bash", "-n", str(HERE.parent / "setup.sh")]).returncode == 0


def main() -> int:
    test_the_pin_is_read_from_setup_sh()
    test_the_refusal()
    test_record_lines_are_refused_off_the_pin_and_dry_lines_never()
    test_setup_sh_is_fatal_on_a_record_box()
    print("test_nc_guard: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
