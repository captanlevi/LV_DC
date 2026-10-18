#!/usr/bin/env python3
"""entry_quality.py (Q am. 1 / am. 10): the tier rule on the coverage snapshot, auto_restored verified from state (the
probe's real localStorage forms), t_settle, and the pick flow on a fake page that answers the module's own JS (menu,
localStorage, the player API, the <video>, content.js's ad flag): a pick and a verified restore; a restore that does not
verify (twitch keeps the manual pick) -> auto_restored false after two clicks; an ad past AD_WAIT_S -> no pick; a
one-rung ladder -> a no-op; a re-entry restores a persisted manual pick; check_session's QA flag.

    python3 test_entry_quality.py        (exit 0 = pass)
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as CS  # noqa: E402
import entry_quality as EQ  # noqa: E402

YT_LS_AUTO = json.dumps({"data": json.dumps({"quality": 0, "previousQuality": 480}), "expiration": 1, "creation": 1})
YT_LS_480 = json.dumps({"data": json.dumps({"quality": 480, "previousQuality": 720}), "expiration": 1, "creation": 1})


def snap(**sessions):
    return {"classes": {c: {"sessions_ge_min": n} for c, n in sessions.items()}}


def test_rule():
    assert [EQ.option_height(x) for x in ("1080p HD", "1440p60\n2k\n(Source)", "480p30", "Auto", "Auto (720p)", None)] == \
        [1080, 1440, 480, None, None, None]
    yt = ["1080p HD", "720p", "480p", "360p", "240p", "144p", "Auto"]
    c = EQ.choose_tier(snap(Q_1080p=5, Q_720p=5, Q_480p=2, Q_360p=4, Q_240p=5, Q_144p=5), yt)
    assert (c["target_label"], c["target_class"], c["deficit"]) == ("480p", "Q_480p", 3.0), c
    c = EQ.choose_tier(snap(Q_1080p=5, Q_720p=3, Q_480p=3, Q_360p=5, Q_240p=5, Q_144p=5), yt)
    assert c["target_label"] == "720p"                                        # a tie goes to the higher tier
    assert EQ.choose_tier(snap(), yt)["target_label"] == "1080p HD"           # an empty snapshot: every class short, tie -> top
    full = {c: 5 for c in ("Q_1080p", "Q_720p", "Q_480p", "Q_360p", "Q_240p", "Q_144p")}
    c = EQ.choose_tier(snap(**full), yt)
    assert c["target_label"] == "1080p HD" and c["why"].startswith("no deficit")
    assert EQ.choose_tier(None, yt)["target_label"] == "1080p HD"           # no snapshot: top
    one = EQ.choose_tier(snap(), ["Auto", "160p", "160p\n(Source)"])           # chaos twitch run 1: one height
    assert one["target_label"] is None and "no-op" in one["why"]
    tw = ["Auto", "1440p60\n2k\n(Source)", "1080p60", "720p60", "480p30", "360p30", "160p30"]
    assert EQ.choose_tier(snap(**{**full, "Q_144p": 1}), tw)["target_label"] == "160p30"


def test_verify():
    items_auto = [{"label": "1080p HD", "checked": "false"}, {"label": "Auto", "checked": "true"}]
    ok, ch = EQ.verify_auto("youtube", {"items": items_auto, "store": {"yt-player-quality": YT_LS_AUTO},
                                         "api": {"preferred": "auto"}})
    assert ok and all(ch.values()), ch
    for bad in ({"items": items_auto, "store": {"yt-player-quality": YT_LS_480}, "api": {"preferred": "auto"}},
                {"items": items_auto, "store": {"yt-player-quality": YT_LS_AUTO}, "api": {"preferred": "large"}},
                {"items": [{"label": "Auto", "checked": "false"}], "store": {"yt-player-quality": YT_LS_AUTO}, "api": {"preferred": "auto"}},
                {"items": items_auto, "store": {}, "api": {"preferred": "auto"}}):
        assert not EQ.verify_auto("youtube", bad)[0], bad
    tw_items = [{"label": "Auto", "checked": True}, {"label": "480p30", "checked": False}]
    assert EQ.verify_auto("twitch", {"items": tw_items, "store": {"video-quality": '{"default":"auto"}'}})[0]
    assert not EQ.verify_auto("twitch", {"items": tw_items, "store": {"video-quality": '{"default":"480p30"}'}})[0]
    assert not EQ.verify_auto("tiktok", {})[0]


def test_settle():
    # chaos yt (probe): 480 until +23 s, the API's target hd720 from +9 s
    yt = [{"t": 100 + k, "w": 854 if k < 23 else 1280, "h": 480 if k < 23 else 720, "q": "large" if k < 9 else "hd720"}
          for k in range(40)]
    assert EQ.t_settle("youtube", yt, 100.0, 480) == 23.0                   # not 0: the API still reported the pick
    same = [{"t": 100 + k, "w": 854, "h": 480, "q": "large"} for k in range(130)]
    assert EQ.t_settle("youtube", same, 100.0, 480) is None                 # Auto kept the picked tier: no change
    tw = [{"t": 200 + k, "w": 852 if k < 5 else 1280, "h": 480 if k < 5 else 720, "q": None} for k in range(30)]
    assert EQ.t_settle("twitch", tw, 200.0) == 5.0
    flat = [{"t": 300 + k, "w": 1280, "h": 720, "q": None} for k in range(130)]
    assert EQ.t_settle("twitch", flat, 300.0) is None and EQ.t_settle("twitch", flat, None) is None


class FakePage:
    """Answers entry_quality's JS: a menu with `options` (+ Auto), localStorage and the API per platform."""

    def __init__(self, platform, options, ad_until=0.0, sticky_manual=False):
        self.platform, self.options, self.ad_until, self.sticky = platform, options, ad_until, sticky_manual
        self.now, self.ct, self.selected, self.clicks = 1000.0, 0.0, "Auto", []
        self.h = 720
        self.store = {"yt-player-quality": YT_LS_AUTO} if platform == "youtube" else {"video-quality": '{"default":"auto"}'}

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def _items(self):
        labs = self.options + ["Auto"] if self.platform == "youtube" else ["Auto"] + self.options
        ch = (lambda lab: "true" if lab == self.selected else "false") if self.platform == "youtube" else (lambda lab: lab == self.selected)
        return [{"label": lab, "checked": ch(lab)} for lab in labs]

    def eval(self, js):
        P = self.platform
        if js == EQ.VIDEO_JS:
            self.ct += 1.0
            return {"ct": self.ct, "paused": False, "rs": 4, "w": self.h * 16 // 9, "h": self.h,
                    "ad": "1" if self.now < self.ad_until else "0"}
        if js == EQ.STORE_JS:
            return dict(self.store)
        if js == EQ.YT_API_JS:
            return {"quality": "hd720", "preferred": "auto" if self.selected == "Auto" else "large", "levels": []}
        if js in (EQ.OPEN_JS[P], EQ.CLOSE_JS[P]):
            return "ok"
        if js == EQ.SUBMENU_JS[P]:
            return "Quality"
        if js == EQ.ITEMS_JS[P]:
            return self._items()
        if js.startswith("(" + EQ.CLICK_JS[P]):
            rx = json.loads(js[js.rindex(")(") + 2:-1])
            lab = next((x["label"] for x in self._items() if re.search(rx, x["label"], re.I)), None)
            self.clicks.append(lab)
            if lab == "Auto" and self.sticky and self.selected != "Auto":
                return lab                                        # the click lands but the manual pick persists
            if lab:
                self.selected = lab
                self.h = EQ.option_height(lab) or 720
                if P == "youtube":
                    self.store["yt-player-quality"] = YT_LS_AUTO if lab == "Auto" else YT_LS_480
                else:
                    self.store["video-quality"] = json.dumps({"default": "auto" if lab == "Auto" else lab.split()[0]})
            return lab
        raise AssertionError(f"unexpected JS: {js[:80]}")


def drv(page):
    return EQ.Driver(page, page.platform, sleep=page.sleep, clock=page.clock)


def test_pick_flows():
    yt_opts = ["1080p HD", "720p", "480p", "360p", "240p", "144p"]
    pg = FakePage("youtube", yt_opts)
    rec = EQ.entry_pick(drv(pg), snap(Q_1080p=5, Q_720p=5, Q_480p=1, Q_360p=5, Q_240p=5, Q_144p=5))
    assert rec["selected"] == "480p" and rec["auto_restored"] is True and rec["auto_clicks"] == 1, rec
    assert rec["t_effect"] is not None and rec["t_pick"] <= rec["t_effect"] <= rec["t_auto"] <= rec["t_verified"]
    assert rec["entry_auto_state"]["auto_checked"] is True and rec["options"][-1] == "Auto" and rec["menu_available"]
    assert pg.clicks == ["480p", "Auto"]
    # twitch: the restore does not verify -> one more click, then auto_restored false (the session is QA-flagged)
    tw_opts = ["1440p60\n2k\n(Source)", "1080p60", "720p60", "480p30", "360p30", "160p30"]
    pg = FakePage("twitch", tw_opts, sticky_manual=True)
    rec = EQ.entry_pick(drv(pg), snap(Q_480p=0))
    assert rec["selected"] == "480p30" and rec["auto_restored"] is False and rec["auto_clicks"] == 2, rec
    # 720p must not match 720p60's sibling: the pick regex is anchored on the whole token
    pg = FakePage("twitch", ["1080p60", "720p60", "720p30", "480p30"])
    assert EQ.entry_pick(drv(pg), snap(Q_720p=0, Q_1080p=5, Q_480p=5))["selected"] == "720p60"
    # an ad longer than AD_WAIT_S: no menu touched
    pg = FakePage("youtube", yt_opts, ad_until=1000.0 + EQ.AD_WAIT_S + 30)
    rec = EQ.entry_pick(drv(pg), snap())
    assert rec["reason"] == "ad" and rec["selected"] is None and rec["auto_restored"] is None and pg.clicks == []
    # the 60 s count from the stream entry: an entry 50 s ago leaves 10 s, and an ad of 30 s more means no pick
    pg = FakePage("youtube", yt_opts, ad_until=1000.0 + 30)
    rec = EQ.entry_pick(drv(pg), snap(), t_entry=1000.0 - 50)
    assert rec["reason"] == "ad" and rec["t_wait_s"] <= 11 and pg.clicks == [], rec
    assert EQ.entry_pick(drv(FakePage("youtube", yt_opts)), snap(), t_entry=1000.0 - 70)["reason"] == "ad"
    # a one-rung ladder: the menu is read, nothing is clicked
    pg = FakePage("twitch", ["160p", "160p\n(Source)"])
    rec = EQ.entry_pick(drv(pg), snap())
    assert rec["menu_available"] is True and rec["selected"] is None and "no-op" in rec["reason"] and pg.clicks == []
    # a re-entry onto a persisted manual pick: restored and verified; onto Auto: nothing clicked
    pg = FakePage("twitch", tw_opts)
    pg.selected, pg.store["video-quality"] = "480p30", '{"default":"480p30"}'
    r = EQ.reentry_check(drv(pg))
    assert r["was_auto"] is False and r["auto_restored"] is True and pg.clicks == ["Auto"], r
    pg = FakePage("youtube", yt_opts)
    assert EQ.reentry_check(drv(pg))["was_auto"] is True and pg.clicks == []
    # the settle trace samples every second for SETTLE_RECORD_S
    tr = EQ.settle_trace(drv(FakePage("youtube", yt_opts)))
    assert len(tr) == int(EQ.SETTLE_RECORD_S) and tr[0]["q"] == "hd720"


def test_check_flag():
    S = CS.entry_quality_summary
    assert S({}) is None
    assert S({"entry_quality": {"selected": "480p", "t_auto": 5.0, "auto_restored": True}})["qa_fail"] is None
    assert S({"entry_quality": {"selected": "480p", "t_auto": 5.0, "auto_restored": False}})["qa_fail"] == "entry"
    assert S({"entry_quality": {"error": "x", "auto_restored": False}})["qa_fail"] == "entry"      # failed mid-way
    assert S({"entry_quality": {"selected": None, "reason": "ad", "auto_restored": None}})["qa_fail"] is None
    m = {"entry_quality": {"selected": "480p", "t_auto": 5.0, "auto_restored": True},
         "frozen_reentries": [{"setup": {"entry_quality": {"was_auto": False, "auto_restored": False}}}]}
    assert S(m)["qa_fail"] == "reentry 0" and S(m)["reentries"] == [False]


def main() -> int:
    test_rule()
    test_verify()
    test_settle()
    test_pick_flows()
    test_check_flag()
    print("test_entry_quality: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
