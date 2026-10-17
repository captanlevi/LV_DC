#!/usr/bin/env python3
"""Screenshot effect on playback for one or more sessions (check_session.shot_effects; reported in check.md too):
dropped frames per tick and client-size changes within 1 s of a shot vs elsewhere. Reads labels.csv and
shots/index.jsonl only.

    python3 shot_effects.py <session_dir> [...]
"""
import json
import sys
from pathlib import Path

from check_session import read_labels, shot_effects

if __name__ == "__main__":
    for a in sys.argv[1:]:
        sd = Path(a)
        print(json.dumps({"session": str(sd), **(shot_effects(read_labels(sd), sd) or {"n/a": "no shots or ticks"})}))
