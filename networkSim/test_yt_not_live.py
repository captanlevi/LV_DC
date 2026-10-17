#!/usr/bin/env python3
"""yt not-live skips read from data/_failed (shagun 2026-10-17): chaos's @Sypherpkvods timeline (yt_7, yt_12, yt_30, all
not live). One failure skips the channel NOT_LIVE_RETRY_S; two within NOT_LIVE_OUT_S put it out for NOT_LIVE_OUT_S
after the latest, so yt_30 would not have been tried; a batch restart keeps the skip (it is on disk).

    python3 test_yt_not_live.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yt_channels as Y  # noqa: E402

NL = "error: RuntimeError: Failed to navigate to a live youtube stream"
T7, T12, T30 = 1792172576.5, 1792181271.9, 1792230676.6     # chaos meta.json started_at


def meta(d: Path, rel: str, **m):
    (d / rel).mkdir(parents=True)
    (d / rel / "meta.json").write_text(json.dumps(m))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        meta(d, "_failed/youtube/yt_7", channel_handle="@Sypherpkvods", started_at=T7, abort_reason=NL)
        meta(d, "_failed/youtube/yt_3", channel_handle="@dwnews", started_at=T7, abort_reason="signal:SIGTERM")
        meta(d, "youtube/yt_8", channel_handle="@RetroWorldVideos", started_at=T7, abort_reason=NL)   # not _failed
        assert set(Y.not_live_skips(d, T7 + 3600)) == {"@Sypherpkvods"}
        assert set(Y.not_live_skips(d, T12)) == {"@Sypherpkvods"}   # yt_12 came 2.4 h later (the in-memory skip was lost)
        assert Y.not_live_skips(d, T7 + Y.NOT_LIVE_RETRY_S + 1) == {}
        meta(d, "_failed/youtube/yt_12", channel_handle="@Sypherpkvods", started_at=T12, abort_reason=NL)
        why = Y.not_live_skips(d, T30)
        assert set(why) == {"@Sypherpkvods"} and why["@Sypherpkvods"].startswith("2 not-live"), why
        assert Y.not_live_skips(d, T12 + Y.NOT_LIVE_OUT_S + 1) == {}
        # the rotation (Sypherpk has since left the list, Addendum P): a never-collected primary is the least-collected
        # one unless skipped; same timeline on @LofiGirl
        for i, c in enumerate(c for c in Y.YT_CHANNELS if c["role"] == "primary" and c["handle"] != "@LofiGirl"):
            meta(d, f"youtube/ok_{i}", channel_handle=c["handle"], started_at=T7)
        for n, t in (("lg_7", T7), ("lg_12", T12)):
            meta(d, f"_failed/youtube/{n}", channel_handle="@LofiGirl", started_at=t, abort_reason=NL)
        assert Y.next_channel(d)["handle"] == "@LofiGirl"
        assert Y.next_channel(d, exclude=set(Y.not_live_skips(d, T30)))["handle"] != "@LofiGirl"
    print("test_yt_not_live: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
