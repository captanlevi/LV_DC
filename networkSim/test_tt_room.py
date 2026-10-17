#!/usr/bin/env python3
"""tt room choice and dead-room rule (browser.py, 2026-10-17): viewer parsing, recent-room exclusion from meta.json,
the TTRoomWatch rule on sample sequences (ti_2's dead player, an ended page, a flat stream, a healthy and a stalling
room), and the page-side JS parses under node. No browser needed (websocket stubbed when absent).

    python3 test_tt_room.py        (exit 0 = pass)
"""
import json
import shutil
import subprocess
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
import browser as B  # noqa: E402


def check(cond, what):
    if not cond:
        raise AssertionError(what)


def test_viewers():
    check(B._parse_viewers("LIVE 1.2K viewers Just chatting") == 1200, "1.2K viewers")
    check(B._parse_viewers("LIVE @tape05.tv 845 watching") == 845, "845 watching")
    check(B._parse_viewers("LIVE gaming 3M") == 3_000_000, "bare 3M token")
    check(B._parse_viewers("LIVE no count here 2024") is None, "a year is not a viewer count")


def test_recent_rooms():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "data"
        for i, (where, room) in enumerate([("tiktok/ti_1", "https://www.tiktok.com/@tape05.tv/live?x=1"),
                                            ("_failed/tiktok/ti_2", "https://www.tiktok.com/@other/live"),
                                            ("tiktok/ti_3", "https://www.tiktok.com/@third/live")]):
            d = root / where
            d.mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps({"channel_url": room, "started_at": 1000 + i}))
        cur = root / "tiktok" / "ti_4"
        cur.mkdir(parents=True)
        got = B.recent_tt_rooms(cur, n=2)
        check(got == ["https://www.tiktok.com/@other/live", "https://www.tiktok.com/@third/live"], f"last 2: {got}")
        check("https://www.tiktok.com/@tape05.tv/live" in B.recent_tt_rooms(cur, n=10), "query string normalised")


def run(samples, dt=1.0):
    w, t = B.TTRoomWatch(), 0.0
    for s in samples:
        r = w.feed(s, t)
        if r:
            return r, t
        t += dt
    return None, t


def test_watch():
    play = lambda i: {"has_video": True, "src": True, "rs": 4, "ct": float(i), "paused": False, "buf_end": i + 3.0}
    check(run([play(i) for i in range(300)])[0] is None, "a healthy room never ends")
    # a stall: playhead frozen while media is still appended (buffer end moves): not a dead room
    stall = [{**play(10), "ct": 10.0, "buf_end": 10.0 + 0.01 * i} for i in range(200)]
    check(run([play(i) for i in range(10)] + stall)[0] is None, "a long stall with media arriving is not room_ended")
    # ti_2's signature: the player's src goes away (readyState 0, paused, 0x0)
    dead = {"has_video": True, "src": False, "rs": 0, "ct": 0.0, "paused": True, "buf_end": None}
    r, t = run([play(i) for i in range(20)] + [dead] * 30)
    check(r == "src_gone" and t >= 20 + B.TT_SRC_GONE_S, f"src gone after {B.TT_SRC_GONE_S} s: {r} {t}")
    # the ended banner with no player: ended_page after TT_ENDED_FLAT_S (before src_gone's 10 s)
    r, t = run([play(i) for i in range(5)] + [{**dead, "ended_text": True}] * 20)
    check(r == "ended_page" and t == 5 + B.TT_ENDED_FLAT_S, f"ended banner, no player: {r} {t}")
    # the banner with a frozen stream: ended_page after TT_ENDED_FLAT_S of no progress
    r, t = run([play(i) for i in range(5)] + [{**play(5), "ended_text": True}] * 20)
    check(r == "ended_page" and t == 6 + B.TT_ENDED_FLAT_S, f"ended banner, stream flat: {r} {t}")   # flat from t = 6
    # the banner text while the room plays (31ded33's failure: a live page's "Recommended LIVE" section) is never dead
    check(run([{**play(i), "ended_text": True} for i in range(120)])[0] is None, "banner text alone while playing")
    # a labelled stall longer than TT_NO_MEDIA_S (tt exposes no buffer) is never a dead room (shagun 2026-10-17)
    stall = {**play(10), "stall_label": True}
    check(run([play(i) for i in range(10)] + [stall] * 200 + [play(i) for i in range(11, 20)])[0] is None,
          "a 200 s labelled stall is not room_ended")
        # paused before autoplay (ti_4 / ti_5: the first sample came before play): flat a few seconds, then plays
    paused = {"has_video": True, "src": True, "rs": 4, "ct": 0.0, "paused": True, "buf_end": 2.0}
    check(run([paused] * 4 + [play(i) for i in range(60)])[0] is None, "paused before autoplay is not dead")
    r, t = run([play(i) for i in range(5)] + [play(5)] * 60)
    check(r == "no_media" and t >= 5 + B.TT_NO_MEDIA_S, f"flat stream {B.TT_NO_MEDIA_S} s: {r} {t}")


def test_ended_phrase_on_real_pages():
    """shagun's DOM snippets (laptop, logged out, 2026-10-17): the rendered "LIVE has ended" banner is on the ended page
    only; "Recommended LIVE" is rendered on both, so it is not an ended signal."""
    from html.parser import HTMLParser

    class Rendered(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.text = [], []

        def handle_starttag(self, tag, attrs):
            self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass

        def handle_data(self, d):
            if d.strip() and not any(t in ("script", "style", "template", "noscript") for t in self.stack):
                self.text.append(d.strip().lower())

    src = (HERE / "fixtures" / "tt_room_html_2026-10-17.html").read_text()
    live = src[src.index("<!-- (a)"):src.index("<!-- (b)")]
    ended = src[src.index("<!-- (b)"):src.index("<!-- (b2)")]
    ended_rec = src[src.index("<!-- (b2)"):]

    def text(h):
        p = Rendered()
        p.feed(h)
        return " ".join(p.text)
    check("live has ended" in text(ended), "the ended page renders the banner")
    check("live has ended" not in text(live), "the live page does not render it")
    check("recommended live" in text(live) and "recommended live" in text(ended_rec), "both render Recommended LIVE")
    check("'recommended live'" not in B._TT_PLAYER_STATE_JS.lower(), "the state JS does not use Recommended LIVE")


def test_js_parses():
    node = shutil.which("node")
    if not node:
        print("SKIP js: node not found")
        return
    for name in ("_TT_CANDIDATES_JS", "_TT_PLAYER_STATE_JS"):
        r = subprocess.run([node, "-e", "new Function(process.argv[1])", getattr(B, name)], capture_output=True, text=True)
        check(r.returncode == 0, f"{name} does not parse: {r.stderr[-300:]}")


if __name__ == "__main__":
    for f in (test_viewers, test_recent_rooms, test_watch, test_ended_phrase_on_real_pages, test_js_parses):
        f()
        print(f"{f.__name__}: ok")
