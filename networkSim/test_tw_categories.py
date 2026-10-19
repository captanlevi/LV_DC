#!/usr/bin/env python3
"""tw_categories.py (addendum O; esports per levi 2026-10-17): least-collected first, pre-rotation sessions count as
Just Chatting, esports only with a live league channel (else skipped and recorded), and the daily summary shows the
skips. No network: the live check is stubbed.

    python3 test_tw_categories.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import batch_summary  # noqa: E402
import tw_categories as T  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for i, cat in enumerate([None, None, "Counter-Strike", "Chess"]):          # 2 pre-rotation sessions
            s = d / "twitch" / f"tw_{i}"
            s.mkdir(parents=True)
            (s / "meta.json").write_text(json.dumps({"tw_category": cat} if cat else {}))
        f = d / "_failed" / "twitch" / "tw_9"
        f.mkdir(parents=True)
        (f / "meta.json").write_text(json.dumps({"tw_category": "Music"}))          # _failed does not count
        counts = T.collected_counts(d)
        assert counts["Just Chatting"] == 2 and counts["Counter-Strike"] == 1 and counts["Music"] == 0, counts
        # the latest session (by started_at) was Chess: never drawn twice in a row, even when least-collected
        (d / "twitch" / "tw_3" / "meta.json").write_text(json.dumps({"tw_category": "Chess", "started_at": 5}))
        assert T.last_category(d) == "Chess"
        assert T.next_category(d, live_check=lambda ch: False)[0]["name"] != "Chess"
        (d / "twitch" / "tw_3" / "meta.json").write_text(json.dumps({"tw_category": "Chess"}))
        c, sk = T.next_category(d, live_check=lambda ch: ch == "lck")
        assert c["type"] == "esports" and c["channel"] == "lck" and sk is None, (c, sk)
        c, sk = T.next_category(d, live_check=lambda ch: False)
        assert c["name"] == "Music" and sk["reason"] == "esports_unavailable", (c, sk)
        assert T.channel_url("lec") == "https://www.twitch.tv/lec" and "Just%20Chatting" in T.url_of("Just Chatting")
        (d / "_summaries").mkdir()
        (d / "_summaries" / "tw_esports_skips.jsonl").write_text(json.dumps({**sk, "ts": time.time()}) + "\n")
        md = batch_summary.write(d).read_text()
        assert "twitch esports draws skipped" in md and "1 today" in md, md[-300:]
    print("test_tw_categories: ok")
    return 0


def test_main():
    """The script's own checks under pytest too (levi t58 T-1: the apply gate runs both runners)."""
    assert main() in (0, None)


if __name__ == "__main__":
    sys.exit(main())
