#!/usr/bin/env python3
"""The twitch category floor (restart prereg §5): every listed category reaches 2 sessions of record before any gets a
4th, ties to the seeded draw (recorded with the pick: meta.json channel_pick.category), the floor before the no-repeat
rule, an unavailable category never blocks (floor_open, recorded), and the channel cap applies within (an esports
league channel at the cap is never drawn). Identity counts span every era (§0: the record flag alone). Synthetic trio
roots; no network (the live check is stubbed).

    python3 test_tw_category_floor.py        (exit 0 = pass)
"""
import json
import random
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import channel_cap as CC  # noqa: E402
import tw_categories as T  # noqa: E402

NAMES = [c["name"] for c in T.TW_CATEGORIES]


def ident(meta):            # §0: identity spans every era (orchestrate._pick's predicate)
    return bool(((meta or {}).get("concurrency") or {}).get("record"))


def _sess(root: Path, i: int, cat: str, channel: str | None = None, era: str = "any", rec: bool = True,
          failed: bool = False) -> None:
    d = root / ("_failed" if failed else "") / "twitch" / f"tw_{100000 + i}"
    d.mkdir(parents=True)
    url = T.channel_url(channel) if channel else f"https://www.twitch.tv/dir_{i}"
    (d / "meta.json").write_text(json.dumps({"tw_category": cat, "started_at": float(i), "channel_url": url,
                                             "concurrency": {"mode": "trio", "record": rec, "era": era}}))
    (d / "check.json").write_text(json.dumps({"verdict": "PASS"}))


def _draw(root: Path, exclude=(), live=lambda ch: True):
    capped = set(CC.cap_state(root, "twitch", ident)["capped"])
    return T.next_category(root, exclude=set(exclude), live_check=live, select=ident, capped=capped)


def test_forward_simulation_all_live():
    """All categories live: the first K draws are a permutation, the first 2K give every category 2, no category ever
    gets a 4th while one is below 2, no immediate repeat, and no esports league channel exceeds the cap."""
    K = len(NAMES)
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        for i in range(4 * K):
            c, sk = _draw(root)
            rec = dict(T.LAST_PICK)
            counts = T.collected_counts(root, ident)
            assert not (min(counts.values()) < T.CATEGORY_FLOOR and counts[c["name"]] >= T.CATEGORY_HOLD_AT), (i, counts, c)
            assert c["name"] != rec["previous"] and rec["stage"] == "rotation" and sk is None, (i, rec)
            if c.get("channel"):
                cap = CC.cap_state(root, "twitch", ident)
                n_ch = sum(1 for m in (root / "twitch").glob("*/meta.json")
                           if json.loads(m.read_text())["channel_url"] == T.channel_url(c["channel"]))
                assert n_ch < cap["cap"], (i, c, cap)                       # the cap within the category
            _sess(root, i, c["name"], c.get("channel"))
            after = T.collected_counts(root, ident)
            if i + 1 == K:
                assert sorted(after.values()) == [1] * K, after
            if i + 1 == 2 * K:
                assert sorted(after.values()) == [2] * K, after
        assert sorted(T.collected_counts(root, ident).values()) == [4] * K


def test_seeded_tie_and_its_record():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        c, _ = _draw(root)
        rec = dict(T.LAST_PICK)
        rnd = random.Random(T.CATEGORY_TIE_SEED)
        key = {n: rnd.random() for n in NAMES}
        first = min(NAMES, key=lambda n: key[n])
        assert rec["seed"] == T.CATEGORY_TIE_SEED and rec["tied"] == sorted(NAMES) and c["name"] == first, rec
        assert (rec["rule"], rec["floor"], rec["hold_at"]) == (T.CATEGORY_RULE, 2, 3)
        assert _draw(root)[0]["name"] == first                               # reproducible from the root's state
        (root / "_failed" / "twitch" / "tw_900000").mkdir(parents=True)     # any outcome advances the seed
        _draw(root)
        assert T.LAST_PICK["seed"] == T.CATEGORY_TIE_SEED + 1
        json.dumps(T.LAST_PICK)                                              # goes to meta.json as is


def test_identity_spans_eras_and_skips_pilots():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        for i, n in enumerate(NAMES):
            _sess(root, i, n, era="t55-gro-off-2026-10-19")                  # an earlier era counts (§0)
        _sess(root, 50, "Chess", rec=False)                                  # a pilot does not
        _sess(root, 51, "Music", failed=True)                                # _failed does not
        assert T.collected_counts(root, ident) == {n: 1 for n in NAMES}


def test_floor_before_the_no_repeat_rule():
    """esports at 1 and just drawn, every other category at 3: a repeat before any 4th."""
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        i = 0
        for n in NAMES:
            if n != "esports":
                for _ in range(3):
                    _sess(root, i, n)
                    i += 1
        _sess(root, i, "esports", "lck")                                     # the latest session
        c, _ = _draw(root)
        assert c["name"] == "esports" and T.LAST_PICK["stage"] == "floor_over_repeat", T.LAST_PICK
        assert T.LAST_PICK["below_floor"] == ["esports"] and "esports" not in T.LAST_PICK["held"]


def test_floor_open_when_the_below_floor_category_is_unavailable():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        i = 0
        for n in NAMES:
            if n != "esports":
                for _ in range(3):
                    _sess(root, i, n)
                    i += 1
        c, sk = _draw(root, live=lambda ch: False)                           # no league channel live
        assert c["name"] != "esports" and T.LAST_PICK["stage"] == "floor_open", T.LAST_PICK
        assert sk["reason"] == "esports_unavailable" and T.LAST_PICK["esports_skip"] == sk
    with tempfile.TemporaryDirectory() as t:                                 # a 2-count category is not held: its
        root = Path(t)                                                       # 3rd before a held category's 4th
        i = 0
        for n in NAMES:
            if n != "esports":
                for _ in range(2 if n == "Chess" else 3):
                    _sess(root, i, n)
                    i += 1
        c, _ = _draw(root, live=lambda ch: False)
        assert c["name"] == "Chess" and T.LAST_PICK["stage"] == "rotation" and "Chess" not in T.LAST_PICK["held"], T.LAST_PICK


def test_the_cap_within_esports():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        chans = T.by_name("esports")["channels"]
        i = 0
        for ch in chans:
            if ch != "lec":
                for _ in range(2):                                           # at the cap (n = 8: cap 2)
                    _sess(root, i, "esports", ch)
                    i += 1
        for n in NAMES:
            if n != "esports":
                for _ in range(2):
                    _sess(root, i, n)
                    i += 1
        _sess(root, i, "Chess")                                              # the latest: not esports
        c, sk = T.next_category(root, exclude=set(NAMES) - {"esports"}, live_check=lambda ch: True, select=ident,
                                capped=set(CC.cap_state(root, "twitch", ident)["capped"]))
        assert c["name"] == "esports" and c["channel"] == "lec", (c, T.LAST_PICK)
        assert len(T.LAST_PICK["cap_excluded"]) == len(chans) - 1
        capped_all = {CC.channel_key(T.channel_url(ch)) for ch in chans}
        c, sk = T.next_category(root, exclude=set(NAMES) - {"esports"}, live_check=lambda ch: True,
                                select=ident, capped=capped_all)                 # every league channel at the cap
        assert c["type"] != "esports" and sk["checked"] == [] and sk["capped"] == sorted(chans), (c, sk)
        assert T.LAST_PICK["stage"] == "all_excluded", T.LAST_PICK


def test_the_wiring():
    import orchestrate as O
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        _sess(root, 0, "Chess")
        _, cat, _ = O._pick("twitch", {"esports": time.time()}, time.time(), False, root=root)   # esports: no network
        assert cat["name"] not in ("Chess", "esports") and cat["pick"]["drawn"] == cat["name"], cat
        assert cat["pick"]["counts"]["Chess"] == 1 and cat["pick"]["excluded"] == ["esports"], cat["pick"]
    orch, exp = (HERE / "orchestrate.py").read_text(), (HERE / "exp.py").read_text()
    assert 'env["LVDC_CHANNEL_PICK"] = json.dumps({"category": category["pick"]}, default=str)' in orch
    assert ("tw_categories.next_category(out, exclude=skip, select=ident, capped=capped,\n"
            "                                                             sibling_held={str(x).lower() for x in siblings})") in orch
    assert '**({"channel_pick": {**channel_pick_start, **browser.channel_pick}}' in exp


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_tw_category_floor: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
