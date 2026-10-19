#!/usr/bin/env python3
"""Trio rotation hygiene (levi / supervisor 2026-10-19, after yt_100007 on @FuboSports): a yt channel / twitch channel /
tt room with 2 trio startup_failures is set aside until a manual check (_summaries/not_usable_cleared.json); only the
trio root is read (single mode unchanged); a yt pick excludes it and records it."""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import not_usable as N  # noqa: E402


def _sess(root: Path, plat: str, name: str, cls: str, t: float, failed: bool = True, **meta):
    d = root / ("_failed" if failed else "") / plat / name
    d.mkdir(parents=True)
    (d / "check.json").write_text(json.dumps({"verdict": "FAIL" if failed else "PASS", "session_class": cls}))
    (d / "meta.json").write_text(json.dumps({"started_at": t, **meta}))


def test_set_aside_and_clear():
    with tempfile.TemporaryDirectory() as d:
        tr = Path(d) / "_trio"
        _sess(tr, "youtube", "yt_100007", "startup_failure", 100.0, channel_handle="@FuboSports")
        assert N.set_aside(tr, "youtube") == {}                                   # one failure: still picked
        _sess(tr, "youtube", "yt_100009", "startup_failure", 200.0, channel_handle="@FuboSports")
        _sess(tr, "youtube", "yt_100010", "normal", 300.0, failed=False, channel_handle="@dwnews")
        assert N.set_aside(tr, "youtube") == {"@FuboSports": ["yt_100007", "yt_100009"]}
        _sess(tr, "twitch", "tw_100011", "startup_failure", 1.0, channel_url="https://www.twitch.tv/RobCdee?x=1")
        _sess(tr, "twitch", "tw_100012", "startup_failure", 2.0, channel_url="https://www.twitch.tv/robcdee")
        assert N.set_aside(tr, "twitch") == {"https://www.twitch.tv/robcdee": ["tw_100011", "tw_100012"]}
        (tr / "_summaries").mkdir()
        (tr / N.CLEARED_FILE).write_text(json.dumps({"@FuboSports": {"cleared_at": 250.0, "by": "shagun"}}))
        assert N.set_aside(tr, "youtube") == {}                                   # manual check: back in the rotation
        _sess(tr, "youtube", "yt_100013", "startup_failure", 400.0, channel_handle="@FuboSports")
        _sess(tr, "youtube", "yt_100014", "startup_failure", 500.0, channel_handle="@FuboSports")
        assert N.set_aside(tr, "youtube") == {"@FuboSports": ["yt_100013", "yt_100014"]}   # failures after the clear
        assert N.trio_root_of(tr / "youtube" / "yt_100099") == tr.resolve()
        assert N.trio_root_of(Path(d) / "youtube" / "yt_99") is None             # single mode: never read


def test_yt_pick_excludes_and_records():
    import orchestrate as O
    import yt_channels as Y
    prim = [c["handle"] for c in Y.YT_CHANNELS if c["role"] == "primary"]
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tr = root / "_trio"
        for k in (1, 2):
            _sess(tr, "youtube", f"yt_10000{k}", "startup_failure", float(k), channel_handle=prim[0])
        saved = O._RUNS_DIR
        try:
            O._RUNS_DIR = root
            import unittest.mock as M
            real = Y.pick_live_channel                                       # no network: every probe reads live
            with M.patch.object(Y, "pick_live_channel", lambda *a, **k: real(*a, **{**k, "probe": lambda h: "live"})):
                ch, _, _ = O._pick("youtube", {}, 1000.0, False, (), tr)
            rec = json.loads((tr / Y.PICKS_FILE).read_text().splitlines()[-1])
            assert ch["handle"] != prim[0] and rec["not_usable"] == {prim[0]: ["yt_100001", "yt_100002"]}, rec
        finally:
            O._RUNS_DIR = saved


if __name__ == "__main__":
    test_set_aside_and_clear()
    test_yt_pick_excludes_and_records()
    print("test_not_usable: ok")
