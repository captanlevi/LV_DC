#!/usr/bin/env python3
"""check_session.shot_effects on a synthetic session: dropped frames rise only on ticks within 1 s of a shot.

    python3 test_shot_effects.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from check_session import read_labels, shot_effects  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        (sd / "shots").mkdir()
        t0, dropped, rows, shots = 1_000_000.0, 0, ["ts,type,client_w,client_h,dropped"], []
        for i in range(100):
            t = t0 + i
            if i % 10 == 0:
                shots.append(t + 0.2)
                dropped += 2                          # a shot costs 2 frames on the tick right after it
            rows.append(f"{t * 1000},res_meta,853,480,{dropped}")
        (sd / "labels.csv").write_text("\n".join(rows) + "\n")
        (sd / "shots" / "index.jsonl").write_text("\n".join(json.dumps({"ts": s}) for s in shots) + "\n")
        r = shot_effects(read_labels(sd), sd)
        assert r["dropped_per_tick_elsewhere"] == 0 and r["dropped_per_tick_near"] > 0.5, r
        assert r["client_size_off_mode_near"] == 0 and r["ticks_near"] == 20, r
    print("test_shot_effects: ok")
    return 0


def test_main():
    """The script's own checks under pytest too (levi t58 T-1: the apply gate runs both runners)."""
    assert main() in (0, None)


if __name__ == "__main__":
    sys.exit(main())
