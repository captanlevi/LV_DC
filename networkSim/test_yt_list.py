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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
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
        # MonsterJam left, FUELTV excluded: the sport spare substitutes (eligible as its type has no primary left; never
        # drawn, so the least recent)
        assert Y.next_channel(d, exclude={"@FUELTVMoto"})["handle"] == "@FuboSports"
        assert "@FuboSports" in Y.LAST_PICK["candidates"] and Y.LAST_PICK["last_draw"]["@FuboSports"] is None, Y.LAST_PICK


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
    """levi 00133's full rule: the least-recently-drawn primary (so never the previous yt session's channel while another
    is possible); trio sessions (data/_trio) never count in single mode's rotation (channels, geometry, twitch
    categories)."""
    import geometry
    import tw_categories
    prim = [c["handle"] for c in Y.YT_CHANNELS if c["role"] == "primary"]
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for i, h in enumerate(prim):                     # every primary twice, the last one once (the least collected)
            for k in range(1 if h == prim[-1] else 2):
                meta(d, f"youtube/yt_{i}{k}", channel_handle=h, started_at=100.0 + i * 10 + k)
        assert Y.next_channel(d, no_repeat=False)["handle"] == prim[0]                # drawn longest ago (t = 101)
        assert Y.LAST_PICK["stages"] == ["lru"] and Y.LAST_PICK["cap"] == 4               # 19 of record: max(2, ceil(3.8))
        meta(d, "youtube/yt_99", channel_handle=prim[-1], started_at=10_000.0)        # it was the latest session
        assert Y.last_channel(d) == prim[-1]
        assert Y.collected_counts(d)[prim[-1]] == 2
        meta(d, "youtube/yt_98", channel_handle=prim[0], started_at=20_000.0)         # now prim[0] is the latest
        c = Y.next_channel(d)
        assert c["handle"] == prim[1] and Y.LAST_PICK["drawn"] == prim[1]               # now the least recent; no repeat
        assert Y.LAST_PICK["seed"] == Y.CHANNEL_TIE_SEED + len(list((d / "youtube").glob("yt_*")))
        only = {c["handle"] for c in Y.YT_CHANNELS} - {prim[0]}
        assert Y.next_channel(d, exclude=only)["handle"] == prim[0]                   # the only one left: it repeats
        meta(d, "_trio/youtube/yt_100001", channel_handle=prim[1], started_at=30_000.0,
             geometry_target="window:1920x1080")
        meta(d, "_trio/twitch/tw_100001", tw_category="Just Chatting", started_at=30_000.0)
        assert Y.last_channel(d) == prim[0] and Y.collected_counts(d)[prim[1]] == 2   # the trio session not counted
        assert geometry.collected_counts(d, "youtube").get("window:1920x1080", 0) == 0
        assert sum(tw_categories.collected_counts(d).values()) == 0
        # levi 00265: the trio's own root counts its sessions (the sessions of record), not single mode's
        tr = d / "_trio"
        assert Y.last_channel(tr) == prim[1] and Y.collected_counts(tr)[prim[1]] == 1 and Y.collected_counts(tr)[prim[0]] == 0
        assert geometry.collected_counts(tr, "youtube").get("window:1920x1080", 0) == 1
        assert tw_categories.collected_counts(tr).get("Just Chatting", 0) == 1
        # levi's note: within the trio root only the sessions of record count (a counted run of the current era)
        import record
        assert Y.collected_counts(tr, record.is_record)[prim[1]] == 0 and Y.last_channel(tr, record.is_record) is None
        meta(d, "_trio/youtube/yt_100002", channel_handle=prim[2], started_at=31_000.0, geometry_target="window:1920x1080",
             concurrency={"mode": "trio", "record": True, "era": record.ERA})
        meta(d, "_trio/twitch/tw_100002", tw_category="Chess", started_at=31_000.0,
             concurrency={"mode": "trio", "record": True, "era": "an earlier era"})
        assert Y.collected_counts(tr, record.is_record)[prim[2]] == 1 and Y.last_channel(tr, record.is_record) == prim[2]
        assert geometry.collected_counts(tr, "youtube", record.is_record).get("window:1920x1080", 0) == 1
        assert sum(tw_categories.collected_counts(tr, record.is_record).values()) == 0
        meta(d, "_failed/youtube/yt_97", channel_handle=prim[2], started_at=40_000.0,
             abort_reason="error: RuntimeError: Failed to navigate to a live youtube stream")
        assert Y.last_channel(d) == prim[0]                                           # a not-live start: no session
        rec = Y.pick_record(d, c)
        assert rec["handle"] == c["handle"] and rec["last"] == prim[0] and rec["no_immediate_repeat"] is True
        assert rec["rule_state"]["rule"] == Y.DIVERSITY_RULE and "seed" in rec["rule_state"]   # the rule, recorded


def test_the_restart_pair_is_broken_by_the_seeded_tie():
    """levi 2026-10-20 (peter 00221): chaos's trio record at b9e53d3 (24 yt session dirs; of record: c1f9181's t55
    RetroWorld, LofiGirl, everybodychillradio, SBC-TV1, ScottishWildlifeGarden, one each) left AlJazeera, dwnews,
    MonsterJam, FUELTVMoto and BeanBoltTV at 0, and list order drew AlJazeera + dwnews, the pair that opened every era.
    Under the full rule the floor keeps the 0-count channels, dwnews (drawn last in the stated run) goes behind the
    never-drawn four, and their seeded tie (seed 24) draws MonsterJam + AlJazeera."""
    import record
    with tempfile.TemporaryDirectory() as d:
        tr = Path(d) / "_trio"
        rec_ch = ["@RetroWorldVideos", "@LofiGirl", "@everybodychillradio", "@SBC-TV1", "@ScottishWildlifeGarden"]
        for i in range(24):
            if i < 5:
                meta(tr, f"youtube/yt_{100001 + i}", channel_handle=rec_ch[i], started_at=float(i),
                     concurrency={"mode": "trio", "record": True, "era": "t55-gro-off-2026-10-19"},
                     lvdc_commit={"sha": "c1f9181"})
            else:
                meta(tr, f"youtube/yt_{100001 + i}", channel_handle="@dwnews", started_at=float(i),
                     concurrency={"mode": "trio", "record": True, "era": "t56-nslot-2026-10-19"})   # a stated run
        a = Y.next_channel(tr, select=record.is_record)
        assert Y.LAST_PICK["seed"] == 24 and Y.LAST_PICK["stages"] == ["floor"] and Y.LAST_PICK["tied"] == sorted(
            ["@AlJazeeraEnglish", "@MonsterJam", "@FUELTVMoto", "@BeanBoltTV"]), Y.LAST_PICK
        b = Y.next_channel(tr, exclude={a["handle"]}, select=record.is_record)
        assert (a["handle"], b["handle"]) == ("@MonsterJam", "@AlJazeeraEnglish"), (a, b)


def test_the_full_rule_forward():
    """levi 00133, simulated forward on an empty trio root (every draw a session of record): the floor gives the first
    F = 10 draws to 10 distinct primaries, LRU then round-robins (3 each after 30 draws); a never-live channel cannot
    hold the floor; the cap passes over a channel at max(2, ceil(2 / F x n_record)) while another is below it."""
    import record
    rec = {"concurrency": {"mode": "trio", "record": True, "era": record.ERA}}
    prim = [c["handle"] for c in Y.YT_CHANNELS if c["role"] == "primary"]
    with tempfile.TemporaryDirectory() as d:
        tr = Path(d) / "_trio"
        drawn = []
        for i in range(30):
            c = Y.next_channel(tr, select=record.is_record)["handle"]
            drawn.append(c)
            meta(tr, f"youtube/yt_{100001 + i}", channel_handle=c, started_at=float(i), **rec)
        assert sorted(drawn[:10]) == sorted(prim) and all(drawn.count(h) == 3 for h in prim), drawn
        assert all(drawn[k] != drawn[k + 1] for k in range(29))
    with tempfile.TemporaryDirectory() as d:                 # FUELTVMoto never live: the floor fills with the other 9
        tr = Path(d) / "_trio"
        drawn = []
        for i in range(12):
            c = Y.next_channel(tr, exclude={"@FUELTVMoto"}, select=record.is_record)["handle"]
            drawn.append(c)
            meta(tr, f"youtube/yt_{100001 + i}", channel_handle=c, started_at=float(i), **rec)
        assert len(set(drawn[:9])) == 9 and "@FUELTVMoto" not in drawn and Y.LAST_PICK["stages"] == ["lru"], drawn
    with tempfile.TemporaryDirectory() as d:                 # the cap: 20 of record, cap max(2, ceil(4.0)) = 4
        tr = Path(d) / "_trio"
        for i in range(20):
            h = prim[0] if i < 5 else prim[1 + (i % 9)]
            meta(tr, f"youtube/yt_{100001 + i}", channel_handle=h, started_at=-1000.0 if h == prim[0] else float(i), **rec)
        c = Y.next_channel(tr, select=record.is_record)
        assert Y.LAST_PICK["cap"] == 4 and "cap" in Y.LAST_PICK["stages"] and c["handle"] != prim[0], Y.LAST_PICK
        others = {c["handle"] for c in Y.YT_CHANNELS} - {prim[0]}
        assert Y.next_channel(tr, exclude=others, select=record.is_record)["handle"] == prim[0]  # alone: the cap yields
        prim_others = {h for h in prim if h != prim[0]}                # the other primaries out: their types' spares in,
        c = Y.next_channel(tr, exclude=prim_others, select=record.is_record)  # and below the cap they go first
        assert c["role"] == "spare" and "cap" in Y.LAST_PICK["stages"], (c, Y.LAST_PICK)


def test_the_rule_on_the_records_draws():
    """The full rule replayed on every prefix of chaos's 27 trio yt draws (fixtures/trio_yt_draws_2026-10-20.json; all
    channels taken as live): it never repeats the previous draw, never takes a channel at the cap while one below is
    eligible, and before boundary-era round 1 (after yt_100023) its first pick is not the AlJazeera / dwnews pair that
    list order drew (yt_100024 / 025)."""
    import record
    draws = json.loads((HERE / "fixtures" / "trio_yt_draws_2026-10-20.json").read_text())["draws"]
    picks = {}
    for k in range(1, len(draws) + 1):
        with tempfile.TemporaryDirectory() as d:
            tr = Path(d) / "_trio"
            for r in draws[:k]:
                meta(tr, f"{'_failed/' if r['failed'] else ''}youtube/{r['session']}", channel_handle=r["handle"],
                     started_at=r["started_at"], concurrency={"record": r["record"], "era": r["era"]},
                     lvdc_commit={"sha": r["sha"]})
            c = Y.next_channel(tr, select=record.is_record)
            st = dict(Y.LAST_PICK)
            assert c["handle"] != draws[k - 1]["handle"], (k, c, st)
            counts = Y.collected_counts(tr, record.is_record)
            if counts[c["handle"]] >= st["cap"]:
                assert all(counts[h] >= st["cap"] for h in st["counts"]), (k, st)
            picks[draws[k - 1]["session"]] = c["handle"]
    assert picks["yt_100023"] not in ("@AlJazeeraEnglish", "@dwnews"), picks["yt_100023"]


def main() -> int:
    test_the_rule_on_the_records_draws()
    test_the_full_rule_forward()
    test_the_restart_pair_is_broken_by_the_seeded_tie()
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
