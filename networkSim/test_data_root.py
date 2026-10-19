#!/usr/bin/env python3
"""data_root.py (supervisor 2026-10-20, before the 4 TB disk): one data-root setting (LVDC_DATA_ROOT, else the repo's
data/) that every module placing or counting sessions reads, the trio staging and floor following it, and exp.py
recording it per session (meta capture.data_root, in both capture writes), so a migration is a move plus this setting.

    python3 test_data_root.py        (exit 0 = pass)
"""
import os
import subprocess
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")


def test_one_root_for_every_module():
    import balance
    import batch_summary
    import coverage_report
    import data_root
    import orchestrate
    import trio_slots
    for name, v in (("orchestrate._RUNS_DIR", orchestrate._RUNS_DIR), ("coverage_report.DATA_DIR", coverage_report.DATA_DIR),
                    ("trio_slots.DATA", trio_slots.DATA), ("balance._DEFAULT_DATA_DIR", balance._DEFAULT_DATA_DIR),
                    ("batch_summary.DATA_DIR", batch_summary.DATA_DIR)):
        assert v is data_root.DATA_ROOT, name
    assert trio_slots.STAGING.parent == data_root.DATA_ROOT or os.environ.get("LVDC_TRIO_STAGING")
    exp_src = (HERE / "exp.py").read_text()
    assert "from data_root import DATA_ROOT as DATA_DIR" in exp_src
    assert exp_src.count('"data_root": data_root_rec') == 2            # both capture writes keep it
    for f in ("preflight.py", "flow_overlap.py"):
        assert 'parent / "data"' not in (HERE / f).read_text(), f


def test_the_setting_and_its_record(tmp_path=None):
    import tempfile
    d = Path(tmp_path or tempfile.mkdtemp())
    code = ("import data_root as D, json; print(json.dumps([str(D.DATA_ROOT), D.record()['source'], D.record()['set']]))")
    env = {**os.environ, "LVDC_DATA_ROOT": str(d)}
    r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    import json
    root, source, setv = json.loads(r.stdout)
    assert root == str(d) and source == "LVDC_DATA_ROOT" and setv == str(d)
    env.pop("LVDC_DATA_ROOT")
    r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True)
    root, source, _ = json.loads(r.stdout)
    assert root == str(HERE.parent / "data") and source == "repo data/"
    import data_root
    rec = data_root.record(d)
    assert rec["resolved"] == str(d.resolve()) and {"set", "source", "resolved"} <= set(rec)


def main() -> int:
    test_one_root_for_every_module()
    test_the_setting_and_its_record()
    print("test_data_root: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
