#!/usr/bin/env python3
"""motion.py (addendum O cross-check) on a synthetic session: player crop, play-only validity (stall / ad / frozen
playhead invalid), MAD of a moving vs a static player, and the n/a rule.

    python3 test_motion.py        (needs Pillow; exit 0 = pass)
"""
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import motion  # noqa: E402
from PIL import Image  # noqa: E402

HDR = "ts,type,video_h,current_time,event_id,is_ad,ad_signals,src_changed\n"


def make(sd: Path, n: int, moving: bool, stall_from: int | None = None) -> None:
    (sd / "shots").mkdir(parents=True)
    rows, idx, t0 = [], [], 1_000_000.0
    for i in range(n):
        t = t0 + 10 * i
        if stall_from is not None and i == stall_from:
            rows.append(f"{(t - 0.5) * 1000},stall,720,{10 * i},{10_000 + i},False,,False")
        ct = 10 * min(i, stall_from) if stall_from is not None and i >= stall_from else 10 * i
        rows.append(f"{t * 1000},res_meta,720,{ct},{i},False,,False")
        im = Image.new("L", (640, 360), 0)
        # the player sits at x 0..400; a bright chat pane at 400..640 changes every shot (must not count)
        shade = (i * 37) % 255 if moving else 90
        im.paste(shade, (20, 20, 380, 220))
        im.paste((i * 91) % 255, (420, 0, 640, 360))
        b = io.BytesIO(); im.convert("RGB").save(b, "JPEG", quality=95)
        name = f"{int(t * 1000)}_tick{i}.jpg"
        (sd / "shots" / name).write_bytes(b.getvalue())
        idx.append({"ts": t, "file": name, "tick": i, "reason": "cadence",
                    "player": {"rect": [0, 0, 400, 240], "inner": [640, 360], "dpr": 1}})
    (sd / "labels.csv").write_text(HDR + "\n".join(rows) + "\n")
    (sd / "shots" / "index.jsonl").write_text("\n".join(json.dumps(x) for x in idx) + "\n")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        a, b, c = Path(td, "a"), Path(td, "b"), Path(td, "c")
        make(a, 40, moving=True)
        make(b, 40, moving=False)
        make(c, 40, moving=True, stall_from=20)
        sa, sb, sc = motion.compute(a), motion.compute(b), motion.compute(c)
        assert sa["status"] == "ok" and sa["valid_pairs"] == 39, sa
        assert sb["status"] == "ok" and sb["session_motion"] < 0.002 < sa["session_motion"], (sa, sb)  # chat ignored
        assert sc["invalid_by_reason"].get("stall", 0) + sc["invalid_by_reason"].get("playhead not advancing", 0) >= 19, sc
        assert sc["status"] == "n/a" and sc["valid_pairs"] < 30, sc
        assert (a / "motion.jsonl").exists() and motion.read_summary(a) == sa
        print(json.dumps({"moving": sa["session_motion"], "static": sb["session_motion"], "stalled": sc["invalid_by_reason"],
                          "pass": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
