#!/usr/bin/env python3
"""levi 11:54 (boundary 3; G7f tw_100014): a twitch slot launched for a league channel a sibling slot held dropped its
first_url (t58 L-7) and walked Just Chatting's directory while meta.tw_channel / tw_category named the league channel,
and the category rotation counts meta.tw_category. Now:
  (a) next_category never draws a league channel a sibling holds (orchestrate.tw_sibling_channels: the siblings' drawn
      channels and their picked / landed streams in the slots log);
  (b) / (c) browser.py's drop is a startup failure (sibling_held; pin_mismatch on a pinned session), never a walk;
  check_session FAILs identity_mismatch when meta.tw_channel is not the channel of meta.channel_url.

    python3 -m pytest test_tw_sibling_held.py
"""
import json
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import check_session as C  # noqa: E402
import tw_categories as T  # noqa: E402

LEAGUE = next(c for c in T.TW_CATEGORIES if c["type"] == "esports")
OTHERS = {c["name"] for c in T.TW_CATEGORIES if c["type"] != "esports"}


def test_next_category_skips_a_sibling_held_league_channel():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        first, second = LEAGUE["channels"][:2]
        c, sk = T.next_category(d, exclude=OTHERS, live_check=lambda ch: True)
        assert c["channel"] == first and sk is None, (c, sk)             # the null: unheld, the first live one
        c, sk = T.next_category(d, exclude=OTHERS, live_check=lambda ch: True, sibling_held={first})
        assert c["type"] == "esports" and c["channel"] == second and sk is None, (c, sk)
        assert T.LAST_PICK["sibling_excluded"] == [first]
        c, sk = T.next_category(d, exclude=OTHERS - {"Just Chatting"}, live_check=lambda ch: True,
                                sibling_held={ch.lower() for ch in LEAGUE["channels"]})
        assert c["type"] != "esports", c                                 # every league channel held: the next type
        assert sk["reason"] == "esports_unavailable" and sk["sibling_held"] == sorted(LEAGUE["channels"]), sk


def test_tw_sibling_channels_reads_drawn_and_landed(monkeypatch):
    import orchestrate as O
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "trio_slots.jsonl"
        rows = [{"event": "start", "slot": "tw1", "session": "tw_1", "platform": "twitch", "run": "R"},
                {"event": "stream", "slot": "tw1", "session": "tw_1", "platform": "twitch", "run": "R",
                 "url": "https://www.twitch.tv/hitch"},
                {"event": "start", "slot": "tw2", "session": "tw_2", "platform": "twitch", "run": "R"},
                {"event": "stream", "slot": "tw2", "session": "tw_2", "platform": "twitch", "run": "R",
                 "url": "https://www.twitch.tv/mine"},
                {"event": "start", "slot": "tw3", "session": "tw_3", "platform": "twitch", "run": "OLD"},
                {"event": "stream", "slot": "tw3", "session": "tw_3", "platform": "twitch", "run": "OLD",
                 "url": "https://www.twitch.tv/oldrun"}]
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        running = {"tw1": {"category": {"name": "esports", "channel": "ESLCS"}},
                   "tw2": {"category": {"name": "esports", "channel": "lck"}},     # this slot: never its own
                   "yt1": {"category": None, "channel": {"handle": "@x"}}}
        assert O.tw_sibling_channels("tw2", running, log, "R") == {"eslcs", "hitch"}


def test_the_sibling_drop_is_a_startup_failure_never_a_walk():
    src = (HERE / "browser.py").read_text()
    i = src.index('self.tw_pick["first_url_held_by_sibling"] = first')
    block = src[i:src.index("nav_first = first", i)]
    assert "raise RuntimeError" in block and "sibling_held" in block and "pin_mismatch" in block
    assert "first = None" not in block


def test_check_session_identity_mismatch():
    url = "https://www.twitch.tv/hitch"
    assert C.tw_identity_mismatch({"tw_channel": "eslcs", "channel_url": url}).startswith("identity_mismatch:")
    assert C.tw_identity_mismatch({"tw_channel": "ESLCS", "channel_url": "https://www.twitch.tv/eslcs/?x=1"}) is None
    assert C.tw_identity_mismatch({"tw_channel": None, "channel_url": url}) is None          # a directory pick
    assert C.tw_identity_mismatch({"tw_channel": "eslcs", "channel_url": None}) is None      # META_REQUIRED's FAIL
    src = (HERE / "check_session.py").read_text()
    assert 'tw_why = tw_identity_mismatch(meta) if platform == "twitch" else None' in src
