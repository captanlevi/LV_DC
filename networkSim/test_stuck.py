#!/usr/bin/env python3
"""The stuck-player rule (Addendum Q am. 5-6, stuck.py) and the fixed frozen measure: synthetic T2 cases (recovers at
80 s -> continue; 90 s above the rung -> re-entry; a second freeze -> censored; below the rung -> no T2), the rung
parsers, chaos tw_1 / tw_27 (fixtures: labels, net_labels, the master playlists' STREAM-INF lines), check_session's
recovered frozen run / censoring, the entries, and BrowserSession._stuck_tick on a fake page.

    python3 test_stuck.py        (exit 0 = pass)
"""
import json
import sys
import threading
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402
import check_session as CS  # noqa: E402
import entries as E  # noqa: E402
import stuck as S  # noqa: E402

FX = HERE / "fixtures"


def nl(*pairs):
    return [{"timestamp": float(t), "speed": float(k), "state": str(st)} for t, k, st in pairs]


def test_t2_cases():
    labels = nl((0, 150, 1), (100, 600, 4))                     # below the 230 rung until 100 s, then above
    # (1) frozen from 120 s: at 200 s the run is 80 s, all above the rung -> under T2 (a recovery here continues)
    t2, st = S.t2_clock(80, 200, labels, 230)
    assert t2 == 80 and st == 120 and t2 < S.T2_S
    # (2) still frozen at 210 s: 90 s above the rung -> T2
    t2, _ = S.t2_clock(90, 210, labels, 230)
    assert t2 >= S.T2_S and S.action(0) == "reenter"
    # (3) a second freeze after the one recovery -> censored
    assert S.action(1) == "censor"
    # (4) a long freeze below the rung: no T2 at all
    assert S.t2_clock(300, 99, labels, 230) == (None, None)
    # the clock starts where the shaper went back above the rung, not at the freeze
    t2, st = S.t2_clock(150, 190, labels, 230)                   # frozen since 40 s, above since 100 s
    assert (t2, st) == (90, 100)
    # a dip below the rung restarts it; an unknown rung disables the rule
    dip = nl((0, 600, 4), (50, 200, 1), (60, 600, 4))
    assert S.above_since(dip, 230, 100) == 60 and S.t2_clock(100, 100, dip, None) == (None, None)
    assert S.shaper_at(dip, 55) == {"state": "1", "kbit": 200.0}


def test_rungs():
    m = "\n".join(['#EXT-X-STREAM-INF:BANDWIDTH=8505600,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2"',
                   '#EXT-X-STREAM-INF:BANDWIDTH=230000,RESOLUTION=284x160,CODECS="avc1.4D401F,mp4a.40.2"',
                   '#EXT-X-STREAM-INF:BANDWIDTH=160000,CODECS="mp4a.40.2"'])                 # audio only: skipped
    assert S.rung_from_master(m) == 230.0 and S.rung_from_master("") is None
    sdk = lambda vc, sfx, vb: ('"sdk_params":"{\\"VCodec\\":\\"%s\\",\\"stream_suffix\\":\\"%s\\",\\"vbitrate\\":%d}"' % (vc, sfx, vb))
    page = ",".join([sdk("h264", "hd", 1800000), sdk("h265", "hd5", 1200000), sdk("h265", "ld5", 400000), sdk("h264", "ao", 0)])
    assert S.rung_from_tt_sdk(page, "h264") == 1800.0 and S.rung_from_tt_sdk(page, "hevc") == 400.0
    fmts = [{"mimeType": "video/webm; codecs=vp9", "bitrate": 110000}, {"mimeType": "video/mp4", "bitrate": 90000},
            {"mimeType": "audio/webm; codecs=opus", "bitrate": 52000}, {"mimeType": "audio/mp4", "bitrate": 130000}]
    assert S.rung_from_yt_formats(fmts) == 90.0 + 52.0
    assert S.rung_from_page("tiktok", json.dumps({"h265": False, "text": page})) == (1800.0, "tt_room_sdk_params", {"codec": "h264"})
    assert S.rung_record("twitch", None, None)["source"] == "fallback" and S.rung_record("twitch", 230.0, "x")["kbit"] == 230.0


def _fx(name):
    d = json.loads((FX / name).read_text())
    return d, [dict(zip(d["columns"], r)) for r in d["rows"]]


def _replay(rows, labels, rung):
    """Second by second over the longest frozen run: (t, frozen_s) when T2 would have fired."""
    a, b = max(CS.frozen_runs(rows), key=lambda r: r[1] - r[0])
    t = a
    while t <= b:
        t2, _ = S.t2_clock(t - a, t, labels, rung)
        if t2 is not None and t2 >= S.T2_S:
            return t, t - a
        t += 1.0
    return None


def test_tw1_tw27():
    """chaos tw_27 (TW-L): frozen ~20 min at 160p; resume presses, re-attaches and a second element's play bursts split
    it under the old stall-only measure (longest 440 s < T 660, the watchdog never fired, check read PASS). The fixed
    measure reads one run past T, and T2 fires once the shaper has been above the 160p rung for 90 s of it."""
    d, rows = _fx("tw27_frozen_2026-10-18.json")
    runs = CS.frozen_runs(rows)
    longest = max(b - a for a, b in runs)
    assert longest > CS.FROZEN_PLAYHEAD_MAX_S["twitch"], longest
    rung = min(x for x in (S.rung_from_master("\n".join(m)) for m in d["master_stream_inf"]) if x)
    assert rung == 230.0
    hit = _replay(rows, d["net_labels"], rung)
    # the shaper sat below the rung (state 1) early in the freeze: the clock starts later, T2 fires ~133 s into the run
    assert hit is not None and S.T2_S <= hit[1] < 140, hit
    # tw_1 (TW-M): the watchdog fired at ~710 s; T2 would have re-entered ~90 s into the freeze
    d, rows = _fx("tw1_frozen_2026-10-18.json")
    assert d["abort_reason"] == "player_frozen"
    hit = _replay(rows, d["net_labels"], 230.0)
    assert hit is not None and hit[1] <= S.T2_S + 1, hit


def test_check_recovered_and_censored():
    d, rows = _fx("tw27_frozen_2026-10-18.json")
    a, b = max(CS.frozen_runs(rows), key=lambda r: r[1] - r[0])
    # a continue re-entry at T2 inside the long run: excluded from player_frozen, reported with the post-recovery part
    cut = [r for r in rows if float(r["ts"]) / 1000 <= a + 95]
    tail = [dict(r, ts=str(float(r["ts"]) + 20_000), current_time=str(10 + i)) for i, r in
            enumerate([r for r in rows if r["type"] == "res_meta" and float(r["ts"]) / 1000 > b - 400][:300])]
    rec = [{"t_nav": a + 92, "outcome": "continue", "t_playing": a + 110, "t2_window": [a + 2, a + 92],
            "rung": {"kbit": 230.0}, "shaper": {"state": "4"}}]
    fp = CS.recovered_frozen(cut + tail, rec)
    assert fp["recovered"] and fp["recovered"]["post_recovery_s"] == 90.0 and fp["max_s"] < CS.FROZEN_PLAYHEAD_MAX_S["twitch"], fp
    lab = CS.label_checks(cut + tail, "twitch", None, rec)
    assert lab["recovered_frozen"] and lab["frozen_playhead_max_s"] < 660
    ok = E.session_class("twitch", "PASS", None, [], None, None, lab["frozen_playhead_max_s"], 660,
                         recovered_frozen=bool(lab["recovered_frozen"]))
    assert ok == "normal"
    # one recovery of either kind: with a player_dead recovery too, nothing is excluded
    assert CS.recovered_frozen(cut + tail, rec, n_dead_recovered=1)["recovered"] is None
    # censored (a second freeze / no resume): the class is player_frozen from the abort
    assert E.session_class("twitch", "FAIL", "player_frozen", [], None, None, 0, 660) == "player_frozen"


def test_entries():
    rows = [{"type": "res_meta", "ts": str(1000 * t), "current_time": str(t), "url": "u", "video_w": "640"} for t in range(100, 400)]
    meta = {"platform": "twitch", "channel_url": "u",
            "frozen_reentries": [{"t_nav": 300.0, "t_enter": 302.0, "url": "u", "outcome": "continue"}],
            "censored": {"frozen_window": [130.0, 260.0], "reason": "second_freeze"}}
    ents = E.compute_entries(rows, meta, nl((0, 600, 4)))
    assert [(e["kind"], e.get("reason"), e.get("outcome")) for e in ents] == [("stream", None, None), ("reentry", "player_frozen", "continue")]
    assert ents[0]["censored_stall"] and not ents[0]["sustained_60s"]          # the censored stall began 30 s after it
    assert not ents[1].get("censored_stall")


class _Page:
    def __init__(self):
        self.url = "https://www.twitch.tv/chan"

    def eval(self, js, await_promise=False):
        return self.url if js == "location.href" else None


def _session(platform="twitch"):
    s = B.BrowserSession.__new__(B.BrowserSession)
    s.platform, s.abort_reason, s.abort_at, s.censored, s.channel_url = platform, None, None, None, "https://www.twitch.tv/chan"
    s.frozen_reentries, s.player_dead_reentries, s.lowest_rung, s._rung_probe_at = [], [], None, 0.0
    s.t2_skipped, s.player_ladder = [], None
    s._recovery_lock, s._page = threading.Lock(), _Page()
    s._master = types.SimpleNamespace(latest={"kbit": 230.0, "t": 0, "path": "/api/v2/channel/hls/chan.m3u8"})
    import time
    now = time.time()
    s.net_labels = nl((now - 500, 600, 4))
    return s


def test_stuck_tick():
    calls = []

    def fake_reenter(self, rec, outcome="continue"):
        calls.append(rec)
        r = {**rec, "outcome": outcome}
        self.frozen_reentries.append(r)
        return r
    s = _session()
    s._reenter_frozen = types.MethodType(fake_reenter, s)
    s._stuck_tick(80)                                            # under T2: nothing
    assert not calls and s.abort_reason is None
    s._stuck_tick(91)                                            # T2: the one re-entry, playback resumed
    assert len(calls) == 1 and s.abort_reason is None and calls[0]["rung"]["source"] == "hls_master_lowest_bandwidth"
    assert calls[0]["shaper"]["kbit"] == 600.0 and calls[0]["t2_s"] >= S.T2_S
    s._stuck_tick(95)                                            # a second freeze: censored, the session ends
    assert s.abort_reason == "player_frozen" and s.censored["reason"] == "second_freeze" and len(calls) == 1
    # a re-entry that does not resume: censored at once
    s2 = _session()
    s2._reenter_frozen = types.MethodType(lambda self, rec: fake_reenter(self, rec, "player_frozen"), s2)
    s2._stuck_tick(120)
    assert s2.abort_reason == "player_frozen" and s2.censored["reason"] == "reentry_not_playing"
    # tt's player_dead re-entry already used the session's recovery: the first stuck player is censored
    s3 = _session()
    s3.player_dead_reentries = [{"outcome": "continue"}]
    s3._reenter_frozen = types.MethodType(fake_reenter, s3)
    calls.clear()
    s3._stuck_tick(100)
    assert not calls and s3.censored["reason"] == "freeze_after_recovery"
    # below the rung: no T2 however long the freeze
    s4 = _session()
    import time
    s4.net_labels = nl((time.time() - 500, 150, 1))
    s4._stuck_tick(400)
    assert s4.abort_reason is None
    # only the fallback rung (the master never read: tw_34, censored at state 4 against 230 kbit): no T2 however long
    # the freeze and however high the shaper; one t2_skipped_rung_unknown record per frozen run (supervisor 2026-10-18)
    s5 = _session()
    s5._master = types.SimpleNamespace(latest=None)
    s5.net_labels = nl((time.time() - 500, 930, 4))
    s5._reenter_frozen = types.MethodType(fake_reenter, s5)
    calls.clear()
    t0 = time.time()
    for f in (60, 95, 96, 97):                                   # one growing run (its start now - frozen_s fixed)
        s5._stuck_tick(f + (time.time() - t0))
    assert s5.abort_reason is None and not calls and s5.lowest_rung["source"] == "fallback"
    assert len(s5.t2_skipped) == 1 and s5.t2_skipped[0]["reason"] == "t2_skipped_rung_unknown", s5.t2_skipped
    assert S.t2_rung({"kbit": 230.0, "source": "fallback"}) is None and S.t2_rung(None) is None
    assert S.t2_rung({"kbit": 230.0, "source": "hls_master_lowest_bandwidth"}) == 230.0
    # the stated exception: no master read, the player's menu lists a 160p option -> twitch's 160p30 transcode rung
    import entry_quality as EQ
    old_state = EQ.Driver.state
    try:
        EQ.Driver.state = lambda self: {"items": [{"label": "Auto"}, {"label": "1080p60\n(Source)"}, {"label": "160p30"}]}
        s6 = _session()
        s6._master = types.SimpleNamespace(latest=None)
        s6._reenter_frozen = types.MethodType(fake_reenter, s6)
        calls.clear()
        s6._stuck_tick(95)
        assert s6.lowest_rung["source"] == "ladder_160p_transcode" and s6.lowest_rung["kbit"] == S.TWITCH_160P_TRANSCODE_KBIT
        assert len(calls) == 1 and not s6.t2_skipped and s6.player_ladder["options"][-1] == "160p30"
        EQ.Driver.state = lambda self: {"items": [{"label": "Auto"}, {"label": "720p60"}, {"label": "480p"}]}
        s7 = _session()
        s7._master = types.SimpleNamespace(latest=None)
        s7._stuck_tick(95)
        assert s7.lowest_rung["source"] == "fallback" and len(s7.t2_skipped) == 1 and s7.abort_reason is None
    finally:
        EQ.Driver.state = old_state


def test_master_listener_reads_the_worker():
    """twitch fetches the master from its player worker (shagun 2026-10-18: no master on 6 of 6 sessions; laptop check:
    captured from a worker session once auto-attached): a worker attach enables Network in that session, and the master
    read there sets the rung."""
    m = B.MasterListener("ws://unused")
    sent = []

    def send(method, params, session_id=None):
        sent.append((method, session_id))
        return len(sent)
    m.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "W1", "targetInfo": {"type": "worker"}}}, send)
    m.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "F1", "targetInfo": {"type": "iframe"}}}, send)
    assert sent == [("Network.enable", "W1")] and m.workers == ["worker"]
    url = "https://usher.ttvnw.net/api/v2/channel/hls/chan.m3u8?allow_source=true"
    m.handle({"method": "Network.responseReceived", "sessionId": "W1", "params": {"requestId": "r1", "response": {"url": url}}}, send)
    m.handle({"method": "Network.loadingFinished", "sessionId": "W1", "params": {"requestId": "r1"}}, send)
    assert sent[-1] == ("Network.getResponseBody", "W1")
    master = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080\nx\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=230000,RESOLUTION=284x160\ny\n')
    m.handle({"id": len(sent), "result": {"body": master, "base64Encoded": False}}, send)
    assert m.latest["kbit"] == 230.0 and m.latest["from_worker"] is True and m.latest["path"].endswith("chan.m3u8")


def main() -> int:
    test_master_listener_reads_the_worker()
    test_t2_cases()
    test_rungs()
    test_tw1_tw27()
    test_check_recovered_and_censored()
    test_entries()
    test_stuck_tick()
    print("test_stuck: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
