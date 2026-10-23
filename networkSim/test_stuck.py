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
    """chaos tw_27 (TW-L): ~20 min at 160p of stalls split by 2-5 s play bursts (resume presses, re-attaches). Under
    addendum N's stall-only run (the 2026-10-18 measure reverted, levi 2026-10-19) the bursts restart the run: longest
    440 s < T 660, a heavy-stall session kept and labelled, not a dead player (levi). T2 still fires once the shaper has
    been above the 160p rung for 90 s of a run."""
    d, rows = _fx("tw27_frozen_2026-10-18.json")
    runs = CS.frozen_runs(rows)
    longest = max(b - a for a, b in runs)
    assert 400 < longest < CS.FROZEN_PLAYHEAD_MAX_S["twitch"], longest
    assert sum(1 for a, b in runs if b - a >= S.T2_S) == 3, runs
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
    # after the re-entry's reload: playback resumes (a start, then 300 advancing ticks), after the cut
    tail = [{"type": "start", "ts": str((a + 99) * 1000), "current_time": "10", "is_ad": "False"}] + [
        {"type": "res_meta", "ts": str((a + 100 + i) * 1000), "current_time": str(10 + i), "is_ad": "False",
         "video_w": "284", "video_h": "160"} for i in range(300)]
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
    s.t2_skipped, s.player_ladder, s.rung_probes = [], None, []
    s._recovery_lock, s._page = threading.Lock(), _Page()
    s._master = types.SimpleNamespace(latest={"kbit": 230.0, "t": 0, "path": "/api/v2/channel/hls/chan.m3u8"})
    import time
    now = time.time()
    s.net_labels = nl((now - 500, 600, 5))     # above the low-state hold (levi 10:27): the T2 rule as before
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
    s5.net_labels = nl((time.time() - 500, 930, 5))
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
        reads = []
        EQ.Driver.state = lambda self: reads.append(1) or {"items": [{"label": "Auto"}, {"label": "1080p60\n(Source)"}, {"label": "160p30"}]}
        s6._stuck_tick(30)                                       # under 60 s frozen: the menu is not touched
        assert not reads and s6.player_ladder is None
        s6._stuck_tick(95)
        assert s6.lowest_rung["source"] == "ladder_160p_transcode" and s6.lowest_rung["kbit"] == S.TWITCH_160P_TRANSCODE_KBIT
        assert len(calls) == 1 and not s6.t2_skipped and s6.player_ladder["options"][-1] == "160p30" and len(reads) == 1
        assert s6.player_ladder["frozen_s"] == 95.0
        s6b = _session()                                         # once per frozen run: later ticks do not re-read
        s6b._master = types.SimpleNamespace(latest=None)
        s6b._reenter_frozen = types.MethodType(fake_reenter, s6b)
        reads.clear()
        t0 = time.time()
        for _ in range(3):
            s6b._current_rung(61 + (time.time() - t0))
        assert len(reads) == 1 and len(s6b.rung_probes) == 1 and s6b.rung_probes[0]["t_open"] <= s6b.rung_probes[0]["t_close"]
        def boom(self):
            reads.append(1)
            raise RuntimeError("menu gone")
        EQ.Driver.state = boom                                   # a read that raises: one retry, then no more
        s6c = _session()
        s6c._master = types.SimpleNamespace(latest=None)
        reads.clear()
        t0 = time.time()
        for _ in range(4):                                       # one run: frozen_s grows with the clock
            s6c._current_rung(61 + (time.time() - t0))
        assert len(reads) == 2 and all(p.get("error") for p in s6c.rung_probes)
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


def test_menu_overlay_shots():
    """levi 2026-10-18: cadence shots within MENU_OVERLAY_S of a ladder probe (meta rung_probes, or player_ladder.t
    for sessions between ef67424 and the probe log) are flagged menu_overlay in check.json; event shots are not."""
    import json as _j
    import tempfile
    from pathlib import Path as P
    with tempfile.TemporaryDirectory() as d:
        sd = P(d)
        (sd / "shots").mkdir()
        rows = [{"ts": 100.0 + 10 * k, "t_decide": 100.0 + 10 * k, "file": f"s{k}.jpg", "reason": "cadence"} for k in range(6)]
        rows.append({"ts": 121.0, "t_decide": 121.0, "file": "ev.jpg", "reason": "tier_change"})
        (sd / "shots" / "index.jsonl").write_text("".join(_j.dumps(r) + "\n" for r in rows))
        assert CS.menu_overlay_shots(sd, {"rung_probes": [{"t_open": 118.0, "t_close": 121.5}]}) == ["s2.jpg"]
        assert CS.menu_overlay_shots(sd, {"player_ladder": {"t": 147.0}}) == ["s5.jpg"]
        assert CS.menu_overlay_shots(sd, {}) == []


def main() -> int:
    test_menu_overlay_shots()
    test_master_listener_reads_the_worker()
    test_t2_cases()
    test_rungs()
    test_tw1_tw27()
    test_check_recovered_and_censored()
    test_entries()
    test_stuck_tick()
    test_low_state_hold()
    test_lowstate_error_page_does_not_use_the_recovery()
    test_free_reentries_bounded_and_hold_availability()
    print("test_stuck: ok")
    return 0


def test_low_state_hold():
    """levi 10:27 (t58 w4, twitch's state-1 fire): on yt / twitch the stuck-player clock holds in states 1-4 and for 30 s
    after leaving them (the frozen run counts only after the hold), the ad_frozen clock restarts while held, and an
    error page that began in the hold re-enters without consuming the one recovery; tt's stuck rule is unchanged."""
    import time
    now = time.time()
    assert S.LOW_HOLD_STATES == set(B.TT_LOW_STATES) and S.LOW_HOLD_GRACE_S == B.TT_STALL_GRACE_S   # tt's hold
    assert S.low_hold_end(nl((now - 500, 600, 7)), now) is None
    assert S.low_hold_end(nl((now - 500, 300, 2)), now) == now + S.LOW_HOLD_GRACE_S           # still low: held
    assert S.low_hold_end(nl((now - 500, 300, 2), (now - 100, 600, 6)), now) == now - 100 + S.LOW_HOLD_GRACE_S
    calls = []

    def fake_reenter(self, rec, outcome="continue"):
        calls.append(rec)
        r = {**rec, "outcome": outcome}
        self.frozen_reentries.append(r)
        return r
    s = _session()
    s.net_labels = nl((now - 500, 600, 3))                       # in state 3 (above the 230 rung): held
    s._reenter_frozen = types.MethodType(fake_reenter, s)
    s._stuck_tick(400)
    assert not calls and s.abort_reason is None
    s.net_labels = nl((now - 500, 600, 3), (now - 100, 600, 6))   # left state 3 100 s ago: counts from 30 s after
    s._stuck_tick(400)                                           # 70 s counted: under T2
    assert not calls
    s.net_labels = nl((now - 500, 600, 3), (now - 130, 600, 6))   # 100 s counted: T2 -> the re-entry
    s._stuck_tick(400)
    assert len(calls) == 1 and calls[0]["frozen_s"] <= 101
    tt = _session("tiktok")                                      # tt: unchanged (its own player_dead hold):
    asked = []                                                   # the hold is never consulted
    real = S.low_hold_end
    S.low_hold_end = lambda *a: asked.append(a) or real(*a)
    try:
        tt.net_labels = nl((now - 500, 2400, 3))
        tt._reenter_frozen = types.MethodType(fake_reenter, tt)
        tt._stuck_tick(100)
        assert asked == []
    finally:
        S.low_hold_end = real
    # the ad_frozen clock: held -> it restarts each tick; unheld -> it fires after AD_FROZEN_MAX_S
    w = B.PlayerErrorWatch(frozen_s=10)
    w.held_fn = lambda t: True
    st = {"ad": True, "video": True, "rs": 4, "ct": 5.0, "cd": "0:21"}
    assert not any(w.step(None, st, 1000.0 + k) for k in range(60))
    w2 = B.PlayerErrorWatch(frozen_s=10)
    w2.held_fn = lambda t: False
    assert any(w2.step(None, st, 1000.0 + k) for k in range(60))


def test_on_frozen_holds_in_low_states():
    """levi gate 49d (C), yt_900027 (00:44 condition 1): addendum N's frozen clock (_on_frozen, T = FROZEN_PLAYHEAD_MAX_S)
    holds on yt / twitch in states 1-4 and for the grace after, as the T2 rule does: no abort while held; past the
    release T counts from the hold's end + grace; unshaped (no labels) and tt unchanged."""
    import time
    now = time.time()
    T, G = CS.FROZEN_PLAYHEAD_MAX_S["youtube"], S.LOW_HOLD_GRACE_S
    yt = _session("youtube")
    for st in (1, 4):                                            # held in 1-4, however long the run (yt_900027: 4)
        yt.net_labels = nl((now - 900, 300, st))
        assert yt._on_frozen(800) is False and yt.abort_reason is None
    yt.net_labels = nl((now - 900, 300, 1), (now - G - T + 20, 600, 6))   # released, T - 20 s counted since end + grace
    assert yt._on_frozen(800) is False and yt.abort_reason is None
    yt.net_labels = nl((now - 900, 300, 1), (now - G - T - 20, 600, 6))   # T + 20 s counted: the abort
    assert yt._on_frozen(800) is True and yt.abort_reason == "player_frozen"
    yt = _session("youtube")                                     # a freeze begun after the hold: its own run counts
    yt.net_labels = nl((now - 900, 300, 1), (now - 800, 600, 6))
    assert yt._on_frozen(T + 5) is True and yt.abort_reason == "player_frozen"
    yt = _session("youtube")                                     # unshaped / startup (no labels): as before
    yt.net_labels, yt.lowstate_hold_available = [], None
    assert yt._on_frozen(T + 5) is True and yt.abort_reason == "player_frozen" and yt.lowstate_hold_available is False
    yt = _session("youtube")                                     # never low: as before
    assert yt._on_frozen(T + 5) is True and yt.abort_reason == "player_frozen"
    Tw = CS.FROZEN_PLAYHEAD_MAX_S["twitch"]                      # twitch: the same at its T
    tw = _session("twitch")
    tw.net_labels = nl((now - 3000, 300, 2), (now - G - Tw + 20, 600, 6))
    assert tw._on_frozen(2500) is False and tw.abort_reason is None
    tw.net_labels = nl((now - 3000, 300, 2), (now - G - Tw - 20, 600, 6))
    assert tw._on_frozen(2500) is True and tw.abort_reason == "player_frozen"
    tt = _session("tiktok")                                      # tt: unchanged (its own player_dead hold)
    tt.net_labels = nl((now - 900, 300, 1))
    assert tt._on_frozen(CS.FROZEN_PLAYHEAD_MAX_S["tiktok"] + 5) is True and tt.abort_reason == "player_frozen"


def test_on_frozen_hold_needs_starvation(monkeypatch):
    """levi 13:09 (tw_100109): addendum N's low-state hold applies only with starvation evidence at the resumer's last
    poll (readyState <= 2 or buffer < 1.5 s). tw_100109's own shaper states (1-4 all session) and poll (paused,
    readyState 4, 4.04 s buffered, stall label on): not held, aborts once the run passes T, the bypass recorded. A
    genuine TW-L starvation stall in the same states (readyState 2, or the buffer under the floor) stays held; no poll,
    or one older than FROZEN_HOLD_POLL_MAX_AGE_S: held as before."""
    fx = json.loads((FX / "tw_100109_selfpause_2026-10-23.json").read_text())
    labels = [{"timestamp": t, "speed": 0.0, "state": st} for t, st in fx["shaper_states"]]
    Tw, now = CS.FROZEN_PLAYHEAD_MAX_S["twitch"], 1100.0
    assert S.low_hold_end(labels, now) >= now                              # tw_100109 at 1100 s: in the hold
    monkeypatch.setattr(B.time, "time", lambda: now)
    frozen = now - fx["paused_run"][0]
    assert frozen > Tw

    def tw(poll, age=1.0):
        s = _session("twitch")
        s.net_labels, s.frozen_hold_bypassed = labels, []
        s.resumer = B.SelfPauseResumer()
        if poll is not None:
            s.resumer.last_poll = (now - age, poll)
        return s
    data = {"paused": True, "rs": 4, "buf": 4.044, "stall": True, "ad": False, "ended": False, "ct": 292.605}
    s = tw(data)
    assert s._on_frozen(frozen) is True and s.abort_reason == "player_frozen"
    assert [{k: b[k] for k in ("rs", "buf", "stall", "paused")} for b in s.frozen_hold_bypassed] == \
        [{"rs": 4, "buf": 4.044, "stall": True, "paused": True}], s.frozen_hold_bypassed
    for poll, age in (({**data, "rs": 2, "buf": 0.09}, 1.0), ({**data, "buf": 0.3}, 1.0), ({**data, "rs": 1}, 1.0),
                      (None, 0.0), (data, B.FROZEN_HOLD_POLL_MAX_AGE_S + 1)):
        s = tw(poll, age)
        assert s._on_frozen(frozen) is False and s.abort_reason is None and s.frozen_hold_bypassed == [], (poll, age)
    s = tw({**data, "buf": B.SELF_PAUSE_MIN_BUFFER_S})                    # at the floor: data, not starvation
    assert s._on_frozen(frozen) is True


def test_watcher_loop_asks_on_frozen_again_while_held():
    """The ad-watch loop calls on_frozen once the run passes T and again each tick while it answers False (held); the
    deadlocked-pause run (resumer.stuck_frozen_s, 3c10fbc; levi 00:44 condition 2) reaches the same gate."""
    for content_js, pause in ((400.0, 0.0), (0.0, 400.0)):
        stop, got = threading.Event(), []

        class P:
            def eval(self, js, await_promise=False):
                return str(content_js) if js == B._FROZEN_JS else None

        class R:
            def step(self, *a):
                return None

            def stuck_frozen_s(self, t):
                return pause

        def on_frozen(f):
            got.append(f)
            if len(got) == 3:
                stop.set()
                return True
            return False
        th = threading.Thread(target=B._ad_watcher_loop, args=(P(), "youtube", stop),
                              kwargs={"on_frozen": on_frozen, "resumer": R()}, daemon=True)
        th.start()
        th.join(timeout=15)
        assert got == [400.0] * 3, (content_js, pause, got)


def _frozen_rows(t0, frozen_from, frozen_to, total):
    """res_meta ticks each second from t0; the stall label on and the playhead still in [frozen_from, frozen_to)."""
    out = [{"type": "start", "ts": str(t0 * 1000)}]
    ct = 0.0
    for k in range(total):
        if k == frozen_from:
            out.append({"type": "stall", "ts": str((t0 + k) * 1000)})
        if k == frozen_to:
            out.append({"type": "start", "ts": str((t0 + k) * 1000)})
        if not frozen_from <= k < frozen_to:
            ct += 1.0
        out.append({"type": "res_meta", "ts": str((t0 + k) * 1000), "current_time": str(ct), "is_ad": "False"})
    return out


def _collector_aborts(platform, run, labels, monkeypatch):
    """Replay the collector's addendum-N clock (browser._on_frozen, 032f6d31) each second of a frozen run: its abort time
    or None."""
    s = _session(platform)
    s.net_labels = labels
    a, b = run
    T = CS.FROZEN_PLAYHEAD_MAX_S[platform]
    for k in range(int(b - a) + 1):
        t = a + k
        if k <= T:                                        # the loop calls on_frozen only once the raw run passes T
            continue
        monkeypatch.setattr(B.time, "time", lambda t=t: t)
        if s._on_frozen(float(k)) is not False:
            return t
    return None


def test_frozen_clock_of_record(monkeypatch):
    """levi 00:59: check_session's yt / twitch frozen class follows the collector's held addendum-N clock on sessions
    from a holding collector (FROZEN_HOLD_COLLECTORS or meta frozen_clock_hold): a held run > T by the labels is not
    player_frozen, the disagreement flagged with its margin; a run past the release counts from release + grace; a
    pre-fix collector, no net labels, or tt: as before. Parity: the held measure exceeds T exactly when the replayed
    collector clock aborts."""
    t0 = 1_000_000.0
    G = S.LOW_HOLD_GRACE_S
    held_sha = sorted(CS.FROZEN_HOLD_COLLECTORS)[0]

    def run(rows, labels, platform="youtube", meta=None):
        lab = CS.label_checks(rows, platform, None, [], None, None, labels)
        CS.frozen_clock_of_record(lab, rows, platform, labels, meta if meta is not None else
                                  {"lvdc_commit": {"sha": held_sha}})
        return lab
    rows = _frozen_rows(t0, 100, 400, 500)                        # a 299 s frozen run, t0+100 .. t0+399
    low = nl((t0, 300, 1))                                        # state 1 the whole session
    lab = run(rows, low)
    assert lab["frozen_playhead_max_s"] == 299.0 and lab["frozen_playhead_class_s"] == 0.0
    assert lab["frozen_clock_of_record"] == {"held": True, "held_s": 0.0, "raw_s": 299.0, "disagrees": True,
                                             "margin_s": 119.0}
    assert CS.E.session_class("youtube", "PASS", None, [], 0.0, 60.0, lab["frozen_playhead_class_s"], 180) == "normal"
    rel = nl((t0, 300, 1), (t0 + 150, 900, 7))                    # released at +150: counts from +180 to +399 = 219 s
    lab = run(rows, rel)
    assert lab["frozen_playhead_class_s"] == 219.0 and not lab["frozen_clock_of_record"]["disagrees"]
    lab = run(rows, nl((t0, 300, 4), (t0 + 250, 900, 7)))          # state 4 held; 399 - 280 = 119 s: under T
    assert lab["frozen_playhead_class_s"] == 119.0 and lab["frozen_clock_of_record"]["disagrees"]
    lab = run(rows, nl((t0, 300, 2), (t0 + 20, 900, 7)))           # released before the run: its own 299 s
    assert lab["frozen_playhead_class_s"] == 299.0
    for meta, labels, plat in (({"lvdc_commit": {"sha": "49d5a19f1bb25e542c97edeba118e75981caa7a8"}}, low, "youtube"),
                               ({"lvdc_commit": {"sha": held_sha}}, [], "youtube"),
                               ({"lvdc_commit": {"sha": held_sha}}, low, "tiktok")):
        lab = run(rows, labels, plat, meta)                       # pre-fix collector / no labels / tt: as before
        assert lab["frozen_playhead_class_s"] == lab["frozen_playhead_max_s"] and lab["frozen_clock_of_record"] is None
    lab = run(rows, low, "twitch", {"lvdc_commit": {"sha": "f" * 40}, "frozen_clock_hold": True})   # the meta marker
    assert lab["frozen_clock_of_record"]["held"] and lab["frozen_playhead_class_s"] == 0.0
    lab = CS.label_checks(rows, "youtube", None, [], None, None, low)   # a recovered run stays out of both measures
    lab["recovered_frozen"] = {"window": [t0 + 100, t0 + 399]}
    CS.frozen_clock_of_record(lab, rows, "youtube", low, {"lvdc_commit": {"sha": held_sha}})
    assert lab["frozen_playhead_class_s"] == 0.0
    # parity with the collector's clock (yt 180, twitch 660), grace edges included
    for plat, n in (("youtube", 500), ("twitch", 1200)):
        T = CS.FROZEN_PLAYHEAD_MAX_S[plat]
        rows = _frozen_rows(t0, 100, n - 1, n)
        a, b = t0 + 100, t0 + n - 2
        for labels in (nl((t0, 300, 1)), nl((t0, 900, 7)), nl((t0, 300, 3), (t0 + 150, 900, 6)),
                       nl((t0, 300, 1), (b - G - T - 1, 900, 6)), nl((t0, 300, 1), (b - G - T + 1, 900, 6)),
                       nl((t0, 300, 1), (b - G - T, 900, 6)), nl((t0, 900, 7), (a + T - 5, 300, 2))):
            lab = run(rows, labels, plat)
            fired = _collector_aborts(plat, (a, b), labels, monkeypatch)
            assert (fired is not None) == CS.E.frozen_over_t(lab["frozen_playhead_class_s"], T), (plat, labels, fired, lab)



def test_hold_edges_every_state(monkeypatch):
    """Hardening stop: on yt and twitch, at every shaper state 1-10, the hold (_lowstate_held, which the T2 stuck clock
    reads too) and the addendum-N clock agree on the grace edge: held while now <= the last low stretch's end +
    LOW_HOLD_GRACE_S (the edge itself included), released 1 s after; after the release T counts from the edge (T
    counted: no abort; T + 1: the abort); states 5+ never hold; tt's hold uses the same grace."""
    import time
    G = S.LOW_HOLD_GRACE_S
    for plat in ("youtube", "twitch"):
        T = CS.FROZEN_PLAYHEAD_MAX_S[plat]
        for st in range(1, 11):
            for d, edge in ((0.0, "at"), (1.0, "after"), (-1.0, "before")):
                now = 1_800_000_000.0
                monkeypatch.setattr(B.time, "time", lambda: now)
                s = _session(plat)
                rel = now - G - d                                  # the stretch ended at rel; hold end = now - d
                s.net_labels = nl((now - 2000, 300, st), (rel, 900, 7))
                held = st <= 4 and d <= 0
                assert s._lowstate_held(now) is held, (plat, st, edge)
                s2 = _session(plat)
                s2.net_labels = s.net_labels
                # addendum N: held, or released d s ago (T counts from the release): no abort; never low: the abort
                assert (s2._on_frozen(T + 1500.0) is False) is (st <= 4), (plat, st, edge)
                for k, aborts in ((0, st > 4), (1, True)):          # T + k s counted since release + grace: > T aborts
                    s3 = _session(plat)
                    s3.net_labels = nl((now - 2000, 300, st), (now - G - T - k, 900, 7))
                    assert s3._on_frozen(T + 1500.0) is aborts, (plat, st, edge, k)
    w = B.TTRoomWatch()                                            # tt: held to last low sample + TT_STALL_GRACE_S
    dead = {"has_video": True, "src": True, "rs": 0, "ct": 0, "buf_end": None, "decoded": None, "stall_label": True}
    w.feed(dead, 1000.0, shaper_state=2)
    assert w.last_low == 1000.0 and B.TT_STALL_GRACE_S == G


def test_first_abort_stands():
    """Hardening stop (coverage audit: _on_wall overwrote): a login wall seen after another abort leaves that abort;
    alone it aborts login_wall. exp.py's exit code reads the same reason meta.json records."""
    s = _session("youtube")
    s.abort_reason, s.abort_at = "player_frozen", 1.0
    s._on_wall()
    assert (s.abort_reason, s.abort_at) == ("player_frozen", 1.0)
    s = _session("youtube")
    s._on_wall()
    assert s.abort_reason == "login_wall"
    exp = (HERE / "exp.py").read_text()
    rule = "(browser.abort_reason if browser is not None else None) or abort_reason"
    assert exp.count(rule) == 2 and exp.index('meta_extra["abort_reason"] = ' + rule) < exp.index(
        "abort_reason = " + rule + "\n    if abort_reason == \"login_wall\"")


def test_not_live_mark_matches_the_start_failure():
    """Coverage audit: session_class reads not_live from abort_reason containing entries.NOT_LIVE_MARK (and yt_channels'
    copy); browser.py's start failure must produce that text for every platform, else not_live reads startup_failure."""
    import yt_channels
    assert E.NOT_LIVE_MARK == yt_channels.NOT_LIVE_MARK
    src = (HERE / "browser.py").read_text()
    raised = __import__("re").findall(r'raise RuntimeError\(f"(Failed to navigate to a live[^"]*)"\)', src)
    assert raised and all(r.startswith(E.NOT_LIVE_MARK) for r in raised), raised
    for p in ("youtube", "twitch"):
        msg = raised[0].replace("{self.platform}", p)
        assert E.session_class(p, "FAIL", msg, [], None, None, None, None) == "not_live"


def test_player_error_accounting(tmp_path):
    """levi 02:57 (a named defect: orchestrate and session_class read player_error two ways; marking channels that err
    under our own low-state shaping not-live biases selection away from stall-rich content): an aborting player error
    begun in the low-state hold exits EXIT_PLAYER_ERROR_HELD, a NOT_A_FAILURE code (no failure streak, no not-live
    mark); one at state >= 5, unshaped, or at startup exits 1 as today (failure streak + not-live mark)."""
    import orchestrate as O
    t0 = 1_800_000_000.0
    err = lambda t, abort=True: [{"reason": "error_page", "since": t, "t_trigger": t + 5, **({"abort": "x"} if abort else {})}]  # noqa: E731
    assert S.player_error_held(err(t0 + 100), nl((t0, 300, 2)))                       # state 2: held
    assert S.player_error_held(err(t0 + 100), nl((t0, 300, 4), (t0 + 80, 900, 7)))    # left 20 s ago: in the grace
    assert not S.player_error_held(err(t0 + 100), nl((t0, 300, 2), (t0 + 50, 900, 7)))  # 50 s after leaving: past it
    assert not S.player_error_held(err(t0 + 100), nl((t0, 900, 7)))                   # state 7
    assert not S.player_error_held(err(t0 + 100), [])                                  # unshaped
    assert not S.player_error_held(err(t0 + 100), nl((t0 + 200, 300, 1)))             # startup: before the first label
    assert not S.player_error_held([], nl((t0, 300, 1))) and not S.player_error_held(None, [])
    two = err(t0 + 10, abort=False) + err(t0 + 500)                                    # the aborting one decides
    assert not S.player_error_held(two, nl((t0, 300, 1), (t0 + 100, 900, 7)))
    assert O.NOT_A_FAILURE[O.EXIT_PLAYER_ERROR_HELD].startswith("player_error") and O.EXIT_PLAYER_ERROR_HELD == 13
    ch = {"handle": "@c"}
    paused, fails, frozen, not_live = {}, {"tw1": 0}, {"tw1": 0}, {}
    O._account("twitch", O.EXIT_PLAYER_ERROR_HELD, 0, ch, None, 5.0, paused, fails, frozen, not_live, tmp_path,
               key="tw1", passed=False)
    assert fails == {"tw1": 0} and not_live == {} and frozen == {"tw1": 1}             # no mark, no failure streak
    O._account("twitch", 1, 0, ch, None, 6.0, paused, fails, frozen, not_live, tmp_path, key="tw1", passed=False)
    assert fails == {"tw1": 1} and not_live == {"@c": 6.0}                              # state >= 5 / startup: as today
    exp = (HERE / "exp.py").read_text()
    assert "EXIT_PLAYER_ERROR_HELD = 13" in exp and "_stuck.player_error_held(browser.player_errors," in exp

def test_lowstate_error_page_does_not_use_the_recovery():
    import time
    now = time.time()
    s = _session()
    s.net_labels = nl((now - 500, 300, 1))
    s.player_errors = []
    s.frozen_reentries = [{"outcome": "continue"}]               # the one recovery is already used
    s._reenter_error = types.MethodType(lambda self, rec: (self.player_dead_reentries.append(r := {**rec,
                                        "outcome": "continue", "method": "reload"}) or r), s)
    s._player_error({"reason": "error_page", "since": now - 10, "t_trigger": now - 5, "error": {"code": "2000"}})
    assert s.abort_reason is None and s.player_dead_reentries[-1]["lowstate_free"] is True
    assert s._recoveries_used() == 1                             # the free re-entry does not count
    s.net_labels = nl((now - 500, 600, 7))                        # out of the hold: the next error aborts
    s._player_error({"reason": "error_page", "since": now - 2, "t_trigger": now, "error": {"code": "2000"}})
    assert s.abort_reason == "player_error"


def test_free_reentries_bounded_and_hold_availability():
    """levi 11:48: at most LOWSTATE_FREE_MAX free re-entries (each recorded with since / t_nav), then the normal rule;
    with no live net labels the hold never applies and lowstate_hold_available reads False."""
    import time
    now = time.time()
    s = _session()
    s.net_labels = nl((now - 900, 300, 1))
    s.player_errors = []
    s.lowstate_hold_available = None
    s._reenter_error = types.MethodType(lambda self, rec: (self.player_dead_reentries.append(r := {**rec,
                                        "t_nav": time.time(), "outcome": "continue", "method": "reload"}) or r), s)
    for k in range(B.LOWSTATE_FREE_MAX):
        s._player_error({"reason": "error_page", "since": now - 60 + k, "t_trigger": now - 55 + k, "error": {}})
    free = [r for r in s.player_dead_reentries if r.get("lowstate_free")]
    assert len(free) == B.LOWSTATE_FREE_MAX and all("since" in r and "t_nav" in r for r in free)
    assert s.abort_reason is None and s._recoveries_used() == 0 and s.lowstate_hold_available is True
    s._player_error({"reason": "error_page", "since": now - 10, "t_trigger": now - 5, "error": {}})
    assert s.abort_reason is None and s._recoveries_used() == 1      # the 4th: the normal one recovery
    s._player_error({"reason": "error_page", "since": now - 2, "t_trigger": now, "error": {}})
    assert s.abort_reason == "player_error"                          # then the abort, as before
    s2 = _session()
    s2.net_labels = []
    s2.lowstate_hold_available = None
    calls = []
    s2._reenter_frozen = types.MethodType(lambda self, rec: (calls.append(rec), self.frozen_reentries.append(
        {**rec, "outcome": "continue"}))[0] or {**rec, "outcome": "continue"}, s2)
    assert s2._lowstate_held(now) is False and s2.lowstate_hold_available is False


if __name__ == "__main__":
    sys.exit(main())
