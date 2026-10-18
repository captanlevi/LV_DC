#!/usr/bin/env python3
"""session_names (supervisor 2026-10-18): a removed session's name is never reused; the counter is persisted, starts at
the highest existing name (accepted or _failed) and never decreases; a name still present is skipped.

    python3 test_session_names.py        (exit 0 = pass)
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_names as S  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for n in (1, 2):
            (d / "twitch" / f"tw_{n}").mkdir(parents=True)
        (d / "_failed" / "twitch" / "tw_5").mkdir(parents=True)
        (d / "twitch" / "notes").mkdir()
        assert S.next_session_dir(d, "twitch", "tw").name == "tw_6"          # first use: above every existing name
        assert json.loads(S.counter_path(d, "twitch").read_text())["last"] == 6
        shutil.rmtree(d / "twitch" / "tw_6")
        shutil.rmtree(d / "_failed" / "twitch" / "tw_5")
        shutil.rmtree(d / "twitch" / "tw_1")
        assert S.next_session_dir(d, "twitch", "tw").name == "tw_7"          # removed names are not reused
        (d / "twitch" / "tw_8").mkdir()                                       # a name still present (a tombstone)
        assert S.next_session_dir(d, "twitch", "tw").name == "tw_9"
        S.counter_path(d, "twitch").write_text(json.dumps({"last": 3}))       # a counter behind the disk never wins
        assert S.next_session_dir(d, "twitch", "tw").name == "tw_10"
        S.counter_path(d, "twitch").write_text("garbled")
        assert S.next_session_dir(d, "twitch", "tw").name == "tw_11"
        assert S.next_session_dir(d, "tiktok", "ti").name == "ti_1"           # per platform
        assert S.counter_path(d, "tiktok").exists() and not list(d.glob("_summaries/*.tmp"))
    print("test_session_names: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
