#!/usr/bin/env python3
"""yt_live_check.py (supervisor 18:35): one plain GET per handle, spaced by --gap (>= 15 s, refused below), logged.

    python3 test_yt_live_check.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import yt_live_check as L  # noqa: E402


def test_gap_and_refusal():
    calls, sleeps = [], []
    saved = (L.Y.yt_live_probe, L.time.sleep, sys.argv)
    L.Y.yt_live_probe = lambda h, errors=None: calls.append(h) or {"state": "live", "top_height": 1080}
    L.time.sleep = lambda s: sleeps.append(s)
    try:
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "log.jsonl"
            sys.argv = ["yt_live_check.py", "@a", "@b", "@c", "--log", str(log), "--gap", "20"]
            assert L.main() == 0
            assert calls == ["@a", "@b", "@c"] and sleeps == [20.0, 20.0]
            assert [json.loads(x)["handle"] for x in log.read_text().splitlines()] == ["@a", "@b", "@c"]
            calls.clear()
            sys.argv = ["yt_live_check.py", "@a", "--log", str(log), "--gap", "5"]
            assert L.main() == 2 and calls == []
    finally:
        L.Y.yt_live_probe, L.time.sleep, sys.argv = saved


def main() -> int:
    test_gap_and_refusal()
    print("test_yt_live_check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
