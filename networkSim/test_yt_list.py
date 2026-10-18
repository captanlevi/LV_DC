#!/usr/bin/env python3
"""yt list maintenance (prereg Addendum P): @Sypherpkvods is off the list (archived in REMOVED_CHANNELS, recorded in
LIST_CHANGES); the collector's leave rule (>= 3 not-live failures and 0 accepted sessions in 48 h) writes the leave to
disk and keeps the channel out after the window; meta.json's list record carries every change; a spare's promotion
check (>= 3 live checks >= 6 h apart spanning 24 h, none not-live inside).

    python3 test_yt_list.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yt_channels as Y  # noqa: E402

NL = "error: RuntimeError: Failed to navigate to a live youtube stream"
H = 3600.0


def meta(d: Path, rel: str, **m):
    (d / rel).mkdir(parents=True)
    (d / rel / "meta.json").write_text(json.dumps(m))


def test_list():
    handles = [c["handle"] for c in Y.YT_CHANNELS]
    assert "@Sypherpkvods" not in handles and [c["handle"] for c in Y.REMOVED_CHANNELS] == ["@Sypherpkvods"]
    assert any(c["handle"] == "@Sypherpkvods" and c["change"] == "removed" for c in Y.LIST_CHANGES)
    gaming = [c for c in Y.YT_CHANNELS if c["type"] == "gaming"]
    assert {(c["handle"], c["role"]) for c in gaming} == {("@RetroWorldVideos", "primary"), ("@BeanBoltTV", "primary")}
    assert any(c["handle"] == "@BeanBoltTV" and c["change"] == "promoted" for c in Y.LIST_CHANGES)    # 2026-10-18


def test_leave_rule():
    with tempfile.TemporaryDirectory() as td:
        d, t0 = Path(td), 1_800_000_000.0
        for i, dt in enumerate((0, 10 * H)):
            meta(d, f"_failed/youtube/yt_{i}", channel_handle="@MonsterJam", started_at=t0 + dt, abort_reason=NL)
        meta(d, "youtube/yt_9", channel_handle="@MonsterJam", started_at=t0 - 49 * H)        # accepted, outside 48 h
        for i, dt in enumerate((0, 10 * H, 30 * H)):                                       # 3 not-live but accepted
            meta(d, f"_failed/youtube/fu_{i}", channel_handle="@FUELTVMoto", started_at=t0 + dt, abort_reason=NL)
        meta(d, "youtube/fu_ok", channel_handle="@FUELTVMoto", started_at=t0 + 20 * H)
        assert Y.apply_leave_rule(d, t0 + 29 * H) == []                                   # 2 failures so far
        meta(d, "_failed/youtube/yt_2", channel_handle="@MonsterJam", started_at=t0 + 30 * H, abort_reason=NL)
        new = Y.apply_leave_rule(d, t0 + 31 * H)
        assert [r["handle"] for r in new] == ["@MonsterJam"] and len(new[0]["not_live_at"]) == 3, new
        assert Y.apply_leave_rule(d, t0 + 32 * H) == []                                   # written once
        # out after the window too, and the list record carries it
        later = t0 + 200 * H
        assert Y.not_live_skips(d, later) == {}                                            # the strikes have expired
        meta(d, "youtube/others", channel_handle="@AlJazeeraEnglish", started_at=t0)
        for c in Y.YT_CHANNELS:                    # everyone else collected twice: MonsterJam would be least-collected
            if c["role"] == "primary" and c["handle"] != "@MonsterJam":
                for k in range(2):
                    meta(d, f"youtube/{c['handle'][1:]}_{k}", channel_handle=c["handle"], started_at=t0)
        assert Y.next_channel(d)["handle"] != "@MonsterJam"
        rec = Y.list_record(d, "@dwnews")
        assert rec["role"] == "primary" and rec["changes"] == Y.LIST_CHANGES
        assert [r["handle"] for r in rec["collector_leaves"]] == ["@MonsterJam"]
        assert rec["version"] == len(Y.LIST_CHANGES) + 1
        assert "@MonsterJam" in {r["handle"] for r in Y.disk_leaves(d)}
        # MonsterJam left, FUELTV excluded: the sport spare substitutes
        ex = {c["handle"] for c in Y.YT_CHANNELS if c["role"] == "primary" and c["type"] != "sport"} | {"@FUELTVMoto"}
        assert Y.next_channel(d, exclude=ex)["handle"] == "@FuboSports"


def test_precheck_observations():
    """levi: not-live pre-checks count; >= 3 observations >= 6 h apart (failures or pre-checks), 0 accepted, 48 h."""
    with tempfile.TemporaryDirectory() as td:
        d, t0 = Path(td), 1_800_000_000.0
        (d / "_summaries").mkdir()
        rows = [{"ts": t0 + dt, "handle": "@dwnews", "type": "news", "role": "primary", "state": st}
                for dt, st in ((0, "not_live"), (2 * H, "not_live"), (7 * H, "not_live"), (8 * H, "unknown"))]
        (d / Y.PRECHECK_SKIPS_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows))
        assert Y.apply_leave_rule(d, t0 + 9 * H) == []                 # 2 h apart counts once; unknown never
        meta(d, "_failed/youtube/yt_5", channel_handle="@dwnews", started_at=t0 + 13 * H, abort_reason=NL)
        new = Y.apply_leave_rule(d, t0 + 14 * H)
        assert [(r["handle"], r["not_live_at"]) for r in new] == [("@dwnews", [t0, t0 + 7 * H, t0 + 13 * H])], new
        # a leave that leaves news with one owner puts its spare up for the live check
        assert Y.spares_needing_promotion(Y.disk_leaves(d)) == ["@channelnewsasia"]


def test_spares_needing_promotion():
    assert Y.spares_needing_promotion([]) == []                                 # BeanBoltTV promoted 2026-10-18
    two = [{"handle": "@MonsterJam"}, {"handle": "@FUELTVMoto"}]
    assert Y.spares_needing_promotion(two) == ["@FuboSports"]


def test_promotion():
    h, t = "@BeanBoltTV", 1_800_000_000.0
    live = lambda dt: {"ts": t + dt, "handle": h, "state": "live"}
    P = Y.promotion_status
    assert not P([live(0), live(6 * H), live(12 * H)], h)["passed"]                       # span 12 h < 24 h
    assert P([live(0), live(12 * H), live(24 * H)], h)["passed"]
    assert not P([live(0), live(3 * H), live(24 * H)], h)["passed"]                       # only 2 >= 6 h apart
    assert P([live(0), live(3 * H), live(9 * H), live(24 * H)], h)["checks"] == [t, t + 9 * H, t + 24 * H]
    off = {"ts": t + 13 * H, "handle": h, "state": "not_live"}
    assert not P([live(0), live(12 * H), off, live(24 * H)], h)["passed"]                 # a not-live inside resets
    unk = {"ts": t + 13 * H, "handle": h, "state": "unknown"}
    assert P([live(0), live(12 * H), unk, live(24 * H)], h)["passed"]                     # unknown is ignored
    assert not P([live(0), live(12 * H), {**live(24 * H), "handle": "@x"}], h)["passed"]


def test_no_immediate_repeat_and_trio_excluded():
    """supervisor 2026-10-19: the least-collected primary, never the previous yt session's channel while another is
    possible; trio sessions (data/_trio) never count in single mode's rotation (channels, geometry, twitch categories)."""
    import geometry
    import tw_categories
    prim = [c["handle"] for c in Y.YT_CHANNELS if c["role"] == "primary"]
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for i, h in enumerate(prim):                     # every primary twice, the last one once (the least collected)
            for k in range(1 if h == prim[-1] else 2):
                meta(d, f"youtube/yt_{i}{k}", channel_handle=h, started_at=100.0 + i * 10 + k)
        assert Y.next_channel(d, no_repeat=False)["handle"] == prim[-1]
        meta(d, "youtube/yt_99", channel_handle=prim[-1], started_at=10_000.0)        # it was the latest session
        assert Y.last_channel(d) == prim[-1]
        assert Y.collected_counts(d)[prim[-1]] == 2
        meta(d, "youtube/yt_98", channel_handle=prim[0], started_at=20_000.0)         # now prim[0] is the latest
        c = Y.next_channel(d)
        assert c["handle"] != prim[0] and c["handle"] == prim[1], c                   # least-collected, no repeat
        only = {c["handle"] for c in Y.YT_CHANNELS} - {prim[0]}
        assert Y.next_channel(d, exclude=only)["handle"] == prim[0]                   # the only one left: it repeats
        meta(d, "_trio/youtube/yt_100001", channel_handle=prim[1], started_at=30_000.0,
             geometry_target="window:1920x1080")
        meta(d, "_trio/twitch/tw_100001", tw_category="Just Chatting", started_at=30_000.0)
        assert Y.last_channel(d) == prim[0] and Y.collected_counts(d)[prim[1]] == 2   # the trio session not counted
        assert geometry.collected_counts(d, "youtube").get("window:1920x1080", 0) == 0
        assert sum(tw_categories.collected_counts(d).values()) == 0
        meta(d, "_failed/youtube/yt_97", channel_handle=prim[2], started_at=40_000.0,
             abort_reason="error: RuntimeError: Failed to navigate to a live youtube stream")
        assert Y.last_channel(d) == prim[0]                                           # a not-live start: no session
        rec = Y.pick_record(d, c)
        assert rec["handle"] == prim[1] and rec["last"] == prim[0] and rec["no_immediate_repeat"] is True


def main() -> int:
    test_no_immediate_repeat_and_trio_excluded()
    test_list()
    test_leave_rule()
    test_precheck_observations()
    test_spares_needing_promotion()
    test_promotion()
    print("test_yt_list: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
