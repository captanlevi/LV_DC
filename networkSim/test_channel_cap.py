#!/usr/bin/env python3
"""The twitch / tt channel cap (levi 2026-10-20; channel_cap.py): open pools, so a cap alone, max(2, ceil(0.10 x
n_record)) sessions of record per channel / host; capped keys join the twitch directory's and the tt room choice's
exclusions and the state goes to meta.json channel_pick. Synthetic trio roots; no browser.

    python3 test_channel_cap.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import channel_cap as C  # noqa: E402
import record  # noqa: E402

REC = {"concurrency": {"mode": "trio", "record": True, "era": record.ERA}}


def _sess(root: Path, plat: str, name: str, url: str, rec: bool = True, failed: bool = False) -> None:
    d = root / ("_failed" if failed else "") / plat / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"channel_url": url, **(REC if rec else {})}))


def test_the_cap():
    with tempfile.TemporaryDirectory() as t:
        tr = Path(t) / "_trio"
        for i in range(25):                       # the record today: 25 twitch sessions, one channel 3 times
            _sess(tr, "twitch", f"tw_{i}", "https://www.twitch.tv/eslcs?x=1" if i < 3 else f"https://www.twitch.tv/c{i}/")
        _sess(tr, "twitch", "tw_90", "https://www.twitch.tv/eslcs", rec=False)        # a pilot: not of record
        _sess(tr, "twitch", "tw_91", "https://www.twitch.tv/eslcs", failed=True)      # _failed: no session of record
        st = C.cap_state(tr, "twitch", record.is_record)
        assert st["n_record"] == 25 and st["cap"] == 3 and st["capped"] == ["https://www.twitch.tv/eslcs"], st
        # restart prereg §0: identity spans every era, so a counted session of a stated era counts toward the cap
        d = tr / "twitch" / "tw_80"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"channel_url": "https://www.twitch.tv/c5",
                                                 "concurrency": {"record": True, "era": "t56-nslot-2026-10-19"}}))
        assert C.cap_state(tr, "twitch", record.is_record)["n_record"] == 25
        st = C.cap_state(tr, "twitch", record.counted_any_era)
        assert st["n_record"] == 26 and st["cap"] == 3, st
        assert st["max_count"] == 3 and st["rule"] == C.CAP_RULE
        for i in range(19):                       # tt today: 19 sessions, max 2 per host: cap max(2, ceil(1.9)) = 2
            _sess(tr, "tiktok", f"ti_{i}", "https://www.tiktok.com/@a/live" if i < 2 else f"https://www.tiktok.com/@h{i}/live")
        st = C.cap_state(tr, "tiktok", record.is_record)
        assert st["cap"] == 2 and st["capped"] == ["https://www.tiktok.com/@a/live"], st
    assert C.cap_state(None, "twitch") is None and C.cap_state(Path("/x"), "youtube") is None   # outside the trio / yt
    with tempfile.TemporaryDirectory() as t:
        assert C.cap_state(Path(t), "twitch", record.is_record)["cap"] == 2                     # empty: the minimum


def test_the_wiring():
    src = (HERE / "browser.py").read_text()
    assert 'cap = channel_cap.cap_state(troot, "twitch", record.counted_any_era)' in src
    assert '| set((cap or {}).get("capped") or [])),' in src                    # twitch: the directory's exclusions
    assert 'cap = channel_cap.cap_state(troot, "tiktok", record.counted_any_era)' in src
    assert '| {_norm_room(k) for k in ((cap or {}).get("capped") or [])})' in src  # tt: the room choice's exclusions
    assert '**({"channel_pick": browser.channel_pick} if browser.channel_pick is not None else {}),' in \
        (HERE / "exp.py").read_text()


def main() -> int:
    test_the_cap()
    test_the_wiring()
    print("test_channel_cap: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
