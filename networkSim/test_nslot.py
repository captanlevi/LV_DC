#!/usr/bin/env python3
"""t56 (design v4 §9): N slots from a composition. The slot table (tags, marks, CDP ports, prios; the mark space's limit),
a run at N = 6 (2 yt, 2 twitch, 2 tt: six slots, each its own tag and session, the composition passed to every slot),
the steering reservation (a class a running sibling of the same platform targets is that much nearer its floor; yt
Q_1080p while another slot holds the fullscreen layout), pick_matrix's reserved deficits and the session record (n,
composition, collector sha). The trio's composition reproduces the trio's commands: test_nslot_equivalence.

    python3 test_nslot.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import orchestrate as O  # noqa: E402
import test_trio_slots as TT  # noqa: E402
import trio_slots as T  # noqa: E402


def test_slot_table():
    six = T.build_slots(T.parse_composition("youtube=2,twitch=2,tiktok=2"))
    assert list(six) == ["yt1", "yt2", "tw1", "tw2", "tt1", "tt2"]
    assert [s.mark for s in six.values()] == list(range(0x11, 0x17)) and [s.prio for s in six.values()] == list(range(10, 16))
    assert [s.cdp_port for s in six.values()] == list(range(9223, 9229)) and 9222 not in [s.cdp_port for s in six.values()]
    assert all(s.prio < T.ORPHAN_PRIO for s in six.values()) and six["yt2"].ifb_in == "lvi_yt2"
    assert T.parse_composition(None) == T.TRIO_COMPOSITION and T.composition_of(six) == {"youtube": 2, "twitch": 2, "tiktok": 2}
    big = T.build_slots({"youtube": 5, "twitch": 5, "tiktok": 5})                 # the mark space: 15 slots
    assert len(big) == T.MAX_SLOTS and max(s.mark for s in big.values()) == 0x1F
    for bad in ({"youtube": 6, "twitch": 5, "tiktok": 5}, {}):
        try:
            T.build_slots(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    for bad in ("youtube=x", "bilibili=1"):
        try:
            T.parse_composition(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_reservation_and_fullscreen_token():
    saved = dict(T.SLOTS)
    T.SLOTS.clear()
    T.SLOTS.update(T.build_slots({"youtube": 2, "twitch": 1}))
    try:
        running = {"yt1": {"target": "Q_720p", "geom": "window:1920x1080"}, "tw1": {"target": "Q_480p", "geom": None}}
        assert O._reserved("yt2", T, running) == {"Q_720p": 1}                    # the yt sibling's, not twitch's
        running["yt1"]["geom"] = "fullscreen:1920x1080"
        assert O._reserved("yt2", T, running) == {"Q_720p": 1, "Q_1080p": 1000}   # the token is held: no Q_1080p
        assert O._reserved("yt1", T, running) == {}                               # its own session never reserves
    finally:
        T.SLOTS.clear()
        T.SLOTS.update(saved)


def test_pick_matrix_reserved():
    import coverage_report as C
    from utils.shaping import PLATFORM_MATRICES
    snap = {"platform": "twitch", "classes": {c: {"sessions_ge_min": 5, "minutes": 50} for c in C.CLASSES["twitch"]},
            "per_matrix": {}, "stall_share_pooled": 0.1}
    snap["classes"]["Q_480p"] = {"sessions_ge_min": 4, "minutes": 9}                # one short
    snap["classes"]["Q_360p"] = {"sessions_ge_min": 3, "minutes": 9}                # two short
    r = C.pick_matrix(snap, PLATFORM_MATRICES["twitch"])
    assert r["target"]["key"] == "Q_360p", r
    r = C.pick_matrix(snap, PLATFORM_MATRICES["twitch"], reserved={"Q_360p": 1})    # a sibling holds one Q_360p
    assert r["target"]["key"] in ("Q_360p", "Q_480p") and r["reserved"] == {"Q_360p": 1}, r
    r = C.pick_matrix(snap, PLATFORM_MATRICES["twitch"], reserved={"Q_360p": 2, "Q_480p": 1})
    assert r["target"] is None and r["default_rotation"], r                         # both deficits held by siblings


def test_run_at_n6():
    with tempfile.TemporaryDirectory() as d:
        spawned = []

        def spawn(cmd, env, log):
            spawned.append((cmd, env, log))
            return TT.FakeProc(1, 0, [])
        rc, calls = TT._trio(Path(d), {"youtube": 5, "twitch": 5, "tiktok": 5}, spawn,
                             slots={"youtube": 2, "twitch": 2, "tiktok": 2})
        tags = [c[c.index("--slot") + 1] for c, _, _ in spawned]
        assert rc == 0 and sorted(tags) == ["tt1", "tt2", "tw1", "tw2", "yt1", "yt2"], tags
        assert all(e["LVDC_TRIO_SLOTS"] == "youtube=2,twitch=2,tiktok=2" for _, e, _ in spawned)
        ev = [json.loads(x) for x in (Path(d) / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
        assert ev[0]["composition"] == {"youtube": 2, "twitch": 2, "tiktok": 2} and len(ev[0]["slots"]) == 6
    assert list(T.SLOTS) == ["yt1", "tw1", "tt1"]                                  # restored for the other tests


def test_session_record():
    rec = T.concurrency_record(T.SLOTS["yt1"])
    assert rec["n"] == 3 and rec["composition"] == T.TRIO_COMPOSITION and rec["tag"] == "yt1"
    assert set(rec["lvdc_commit"]) >= {"sha", "dirty"}


def main() -> int:
    test_slot_table()
    test_reservation_and_fullscreen_token()
    test_pick_matrix_reserved()
    test_run_at_n6()
    test_session_record()
    print("test_nslot: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
