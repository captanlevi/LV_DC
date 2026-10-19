#!/usr/bin/env python3
"""t56 (design v4 §9): N slots from a composition. The slot table (tags, marks, CDP ports, prios; the mark space's limit),
a run at N = 6 (2 yt, 2 twitch, 2 tt: six slots, each its own tag and session, the composition passed to every slot),
the steering reservation (a class a running sibling of the same platform targets is that much nearer its floor; yt
Q_1080p while another slot holds the fullscreen layout), pick_matrix's reserved deficits and the session record (n,
composition, collector sha). The trio's composition reproduces the trio's commands: test_nslot_equivalence.

    python3 test_nslot.py        (exit 0 = pass)
"""
import json
import os
import sys
import tempfile
import types
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
    PLATFORM_MATRICES = {"twitch": {m: v for m, v in PLATFORM_MATRICES["twitch"].items() if "@" not in m}}  # base only
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


def test_sibling_streams():
    """levi (t56): a slot's landed stream goes to the run's slots log; a refill's choice sees its running siblings'
    streams of the same platform (not its own, not an ended slot's, not another run's)."""
    try:
        import websocket  # noqa: F401
    except ImportError:                                    # no browser needed: websocket stubbed when absent
        sys.modules["websocket"] = types.ModuleType("websocket")
    import browser as B
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "_trio"
        sd = root / "tiktok" / "ti_100010"
        sd.mkdir(parents=True)
        log = root / "_summaries" / "trio_slots.jsonl"
        log.parent.mkdir(parents=True)
        ev = [{"event": "start", "slot": "tt1", "session": "ti_100008", "platform": "tiktok", "run": "R"},
              {"event": "start", "slot": "tt3", "session": "ti_100009", "platform": "tiktok", "run": "R"},
              {"event": "start", "slot": "tt9", "session": "ti_000001", "platform": "tiktok", "run": "OLD"},
              {"event": "stream", "slot": "tt9", "session": "ti_000001", "platform": "tiktok", "url": "https://www.tiktok.com/@old/live", "run": "OLD"},
              {"event": "start", "slot": "yt1", "session": "yt_100011", "platform": "youtube", "run": "R"}]
        log.write_text("".join(json.dumps(e) + "\n" for e in ev))
        env = {k: os.environ.get(k) for k in ("LVDC_TRIO_RUN", "LVDC_SLOT_TAG")}
        try:
            os.environ.update(LVDC_TRIO_RUN="R", LVDC_SLOT_TAG="tt1")
            B.publish_stream(root / "tiktok" / "ti_100008", "tiktok", "https://www.tiktok.com/@Mine/live?x=1")
            os.environ["LVDC_SLOT_TAG"] = "tt3"
            B.publish_stream(root / "tiktok" / "ti_100009", "tiktok", "https://www.tiktok.com/@jakeytv/live")
            os.environ["LVDC_SLOT_TAG"] = "yt1"
            B.publish_stream(root / "youtube" / "yt_100011", "youtube", "https://www.youtube.com/@x/live")
            os.environ["LVDC_SLOT_TAG"] = "tt2"
            assert B.sibling_streams(sd, "tiktok") == ["https://www.tiktok.com/@jakeytv/live",
                                                       "https://www.tiktok.com/@mine/live"], B.sibling_streams(sd, "tiktok")
            with open(log, "a") as fh:
                fh.write(json.dumps({"event": "end", "slot": "tt3", "session": "ti_100009"}) + "\n")
            assert B.sibling_streams(sd, "tiktok") == ["https://www.tiktok.com/@mine/live"]
            os.environ["LVDC_SLOT_TAG"] = "tt1"
            assert B.sibling_streams(sd, "tiktok") == []                                   # its own stream: not a sibling's
            os.environ.pop("LVDC_TRIO_RUN")
            assert B.sibling_streams(sd, "tiktok") == [] and B._slots_log(sd) is None      # single mode: nothing
        finally:
            for k, v in env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_yt_sibling_exclusion_and_long_not_live():
    """A yt refill excludes its running siblings' channels (recorded); a primary pre-checked not live for >= 6 h is
    unavailable for the tie (recorded) until a re-entry probe reads it live."""
    import yt_channels as Y
    prim = [c["handle"] for c in Y.YT_CHANNELS if c["role"] == "primary"]
    a, b = prim[0], prim[1]
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        f = root / Y.PRECHECK_SKIPS_FILE
        f.parent.mkdir(parents=True)
        now = 100_000.0
        f.write_text("".join(json.dumps({"ts": now - h * 3600, "handle": a, "state": "not_live"}) + "\n" for h in (7, 4, 1)))
        assert list(Y.long_not_live([root], now)) == [a] and Y.long_not_live([root], now)[a]["n"] == 3
        assert Y.long_not_live([root], now - 2 * 3600) == {}                              # the span was under 6 h then
        with open(f, "a") as fh:
            fh.write(json.dumps({"ts": now - 0.5 * 3600, "handle": a, "state": "live"}) + "\n")
        assert Y.long_not_live([root], now) == {}                                         # seen live: re-entered
        f.write_text("".join(json.dumps({"ts": now - h * 3600, "handle": a, "state": "not_live"}) + "\n" for h in (7, 4, 1)))
        saved = O._RUNS_DIR
        try:
            O._RUNS_DIR = root
            trio = root / "_trio"
            trio.mkdir()
            probed = []

            def probe(h):
                probed.append(h)
                return "not_live" if h == a else "live"
            ch, _, _ = O._pick("youtube", {}, now, False, (), trio, siblings={b}, probe=probe)
            rec = json.loads((trio / Y.PICKS_FILE).read_text().splitlines()[-1])
            assert ch["handle"] not in (a, b) and rec["sibling_excluded"] == [b] and a in rec["unavailable_not_live_6h"], rec
            assert probed[0] == a                                                          # the re-entry probe first
            ch, _, _ = O._pick("youtube", {}, now, False, (), trio, probe=lambda h: "live")   # read live: back at once
            rec = json.loads((trio / Y.PICKS_FILE).read_text().splitlines()[-1])
            # back in the draw at once (levi 2026-10-20: the tie among 0-count primaries is the seeded shuffle, recorded)
            assert "unavailable_not_live_6h" not in rec and a in rec["rule_state"]["candidates"], rec
            assert ch["handle"] in rec["rule_state"]["tied"] and ch["pick"]["rule_state"]["seed"] == Y.CHANNEL_TIE_SEED + 0, ch
        finally:
            O._RUNS_DIR = saved


def test_exp_accepts_the_composition_tags():
    """shagun's N = 4 gate at 1941f80: exp.py's --slot still offered the trio's tags only (choices yt / tw / tt), so
    every slot's exp.py exited rc 2 and decode_stress ran no session. Now: no fixed choices; trio_slots.slot_arg_error
    takes any tag of the composition on its own platform with --auto."""
    src = (HERE / "exp.py").read_text()
    i = src.index('"--slot",')
    assert "choices" not in src[i:src.index(")", i)] and "slot_arg_error(args.slot, platform, args.auto)" in src
    saved = dict(T.SLOTS)
    try:
        T.SLOTS.clear()
        T.SLOTS.update(T.build_slots({"youtube": 2, "twitch": 1, "tiktok": 1}))
        assert T.slot_arg_error("yt2", "youtube", True) is None and T.slot_arg_error("tt1", "tiktok", True) is None
        assert "not a slot of the composition" in T.slot_arg_error("yt3", "youtube", True)
        assert "not a slot of the composition" in T.slot_arg_error("yt", "youtube", True)     # the trio's old tag
        assert "is the youtube slot" in T.slot_arg_error("yt1", "twitch", True)
        assert "needs --auto" in T.slot_arg_error("tw1", "twitch", False)
    finally:
        T.SLOTS.clear()
        T.SLOTS.update(saved)


def test_a_pick_is_claimed_before_it_lands():
    """t58 N-2 (brad's audit): a pick is claimed at once ("intent" in the slots log), before tt's confirm / preroll or
    twitch's playback check, so a sibling refilling meanwhile excludes it; the slot's later intent (the next try) or its
    landed stream replaces the claim. _navigate_to_live calls claim() with the twitch directory pick before the
    playback check."""
    try:
        import websocket  # noqa: F401
    except ImportError:
        sys.modules["websocket"] = types.ModuleType("websocket")
    import browser as B
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "_trio"
        log = root / "_summaries" / "trio_slots.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("".join(json.dumps(e) + "\n" for e in (
            {"event": "start", "slot": "tt1", "session": "ti_100020", "platform": "tiktok", "run": "R"},
            {"event": "start", "slot": "tt2", "session": "ti_100021", "platform": "tiktok", "run": "R"})))
        env = {k: os.environ.get(k) for k in ("LVDC_TRIO_RUN", "LVDC_SLOT_TAG")}
        try:
            os.environ.update(LVDC_TRIO_RUN="R", LVDC_SLOT_TAG="tt1")
            mine = root / "tiktok" / "ti_100020"
            B.publish_stream(mine, "tiktok", "https://www.tiktok.com/@a/live", "intent")      # first try, in preroll
            os.environ["LVDC_SLOT_TAG"] = "tt2"
            other = root / "tiktok" / "ti_100021"
            assert B.sibling_streams(other, "tiktok") == ["https://www.tiktok.com/@a/live"]
            os.environ["LVDC_SLOT_TAG"] = "tt1"
            B.publish_stream(mine, "tiktok", "https://www.tiktok.com/@b/live", "intent")      # @a failed: next try
            os.environ["LVDC_SLOT_TAG"] = "tt2"
            assert B.sibling_streams(other, "tiktok") == ["https://www.tiktok.com/@b/live"]
            os.environ["LVDC_SLOT_TAG"] = "tt1"
            B.publish_stream(mine, "tiktok", "https://www.tiktok.com/@b/live")                # landed
            os.environ["LVDC_SLOT_TAG"] = "tt2"
            assert B.sibling_streams(other, "tiktok") == ["https://www.tiktok.com/@b/live"]
            ev = [json.loads(x) for x in log.read_text().splitlines()]
            assert [e["event"] for e in ev[2:]] == ["intent", "intent", "stream"], ev
        finally:
            for k, v in env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    calls = []

    class Page:
        def navigate(self, url):
            calls.append(("navigate", url))

        def eval(self, js):
            if js == "location.href":
                return "https://www.twitch.tv/directory/all"
            if "exclude" in js and "candidates" in js:
                return json.dumps({"pick": "https://www.twitch.tv/somechan", "candidates": ["https://www.twitch.tv/somechan"]})
            return True
    saved = (B._wait_for_load, B._video_is_advancing, B.LIVE_URLS.get("twitch"))
    B._wait_for_load, B._video_is_advancing = (lambda *a, **k: None), (lambda page: calls.append(("check", None)) or False)
    B.LIVE_URLS["twitch"] = ["https://www.twitch.tv/directory/all"]
    try:
        B._navigate_to_live(Page(), "twitch", timeout=0.05, exclude=[], record={},
                            claim=lambda u: calls.append(("claim", u)))
    finally:
        B._wait_for_load, B._video_is_advancing, B.LIVE_URLS["twitch"] = saved
    order = [c[0] for c in calls]
    assert ("claim", "https://www.twitch.tv/somechan") in calls and order.index("claim") < order.index("check"), calls


def test_tt_idle_rule_reads_the_slots_own_ifb():
    """t58 L-1 (brad's audit): in a slot, tt's room-end rule (b) reads the slot's own ingress IFB (exp.py sets
    SHAPED_IFACE = lvi_<tag> and RX_OWN_IFB); with every interface summed, a sibling's stream kept the idle test false
    and a dead room never ended. Single mode keeps every interface; meta records the source (room_rx_source)."""
    try:
        import websocket  # noqa: F401
    except ImportError:
        sys.modules["websocket"] = types.ModuleType("websocket")
    import browser as B
    assert B.RX_OWN_IFB is False and B.room_rx_source() == "all_interfaces"
    w_own, w_all = B.TTRoomWatch(t_max_s=60), B.TTRoomWatch(t_max_s=60)
    flat = {"has_video": True, "src": "blob:x", "rs": 4, "buf_end": 5.0, "ct": 3.0, "decoded": 100}
    out_own = out_all = None
    for i in range(130):                                   # the slot's own class idle; a sibling streams 500 kB/s
        t = 1000.0 + i
        out_own = out_own or w_own.feed(dict(flat), t, shaper_state=8, rx_bytes=10_000, shaped_rx_bytes=10_000)
        out_all = out_all or w_all.feed(dict(flat), t, shaper_state=8, rx_bytes=10_000 + 500_000 * i)
    assert out_own == "no_progress" and out_all is None, (out_own, out_all)
    src = (Path(__file__).resolve().parent / "exp.py").read_text()
    assert "_browser_mod.SHAPED_IFACE, _browser_mod.RX_OWN_IFB = SLOT.ifb_in, True" in src
    assert '"room_rx_source": _room_rx_source()' in src and "return _b.room_rx_source()" in src
    bsrc = (Path(__file__).resolve().parent / "browser.py").read_text()
    assert "rx_bytes=own if RX_OWN_IFB else _rx_bytes()" in bsrc
    saved = (B.SHAPED_IFACE, B.RX_OWN_IFB)
    try:
        B.SHAPED_IFACE, B.RX_OWN_IFB = T.SLOTS["tt1"].ifb_in, True
        assert B.room_rx_source() == "lvi_tt1"
    finally:
        B.SHAPED_IFACE, B.RX_OWN_IFB = saved


def test_mgmt_udp_port_is_the_makefiles_live_lookup():
    """t58 L-6 (brad's audit): the trio's management bypass takes tailscaled's live UDP port as the Makefile reads it
    (sudo -n ss -ulpnH), recorded with its source in meta concurrency; an override (tests) and the 41641 fallback."""
    import subprocess as sp
    saved, env = sp.run, os.environ.pop("LVDC_MGMT_UDP_PORT", None)
    try:
        sp.run = lambda *a, **k: types.SimpleNamespace(stdout="UNCONN 0 0 0.0.0.0:41999 0.0.0.0:* users:((\"tailscaled\",pid=9))\n")
        assert T.mgmt_udp_port_record() == {"port": 41999, "source": "tailscaled (ss -ulpnH)"}
        assert "match ip dport 41999 0xffff" in " ".join(T.shared_up_cmds())
        sp.run = lambda *a, **k: types.SimpleNamespace(stdout="UNCONN 0 0 0.0.0.0:5353 0.0.0.0:* users:((\"avahi\",pid=1))\n")
        assert T.mgmt_udp_port_record()["port"] == T.MGMT_UDP_PORT
        os.environ["LVDC_MGMT_UDP_PORT"] = "40000"
        assert T.mgmt_udp_port_record() == {"port": 40000, "source": "LVDC_MGMT_UDP_PORT"}
    finally:
        sp.run = saved
        os.environ.pop("LVDC_MGMT_UDP_PORT", None)
        if env is not None:
            os.environ["LVDC_MGMT_UDP_PORT"] = env


def test_twitch_history_reads_both_roots_and_skips_uncategorised_running():
    """t58 L-7: recent twitch channels come from both roots (single and trio), as tt rooms do; a running session whose
    category is not written yet is not counted as Just Chatting; a channel page a sibling plays is never taken (the
    category pick instead, recorded)."""
    try:
        import websocket  # noqa: F401
    except ImportError:
        sys.modules["websocket"] = types.ModuleType("websocket")
    import browser as B
    import tw_categories as TC
    with tempfile.TemporaryDirectory() as d:
        data = Path(d)
        for rel, t, url, cat, done in (("twitch/tw_1", 1.0, "https://www.twitch.tv/single", "Chess", True),
                                       ("_trio/twitch/tw_100001", 2.0, "https://www.twitch.tv/trio", None, False),
                                       ("_trio/_failed/twitch/tw_100002", 3.0, "https://www.twitch.tv/failed", None, True)):
            (data / rel).mkdir(parents=True)
            (data / rel / "meta.json").write_text(json.dumps({"started_at": t, "channel_url": url,
                                                              **({"tw_category": cat} if cat else {})}))
            if done:
                (data / rel / "check.json").write_text("{}")
        me = data / "_trio" / "twitch" / "tw_100003"
        assert B.recent_tw_channels(me) == ["https://www.twitch.tv/failed", "https://www.twitch.tv/single",
                                            "https://www.twitch.tv/trio"], B.recent_tw_channels(me)
        counts = TC.collected_counts(data / "_trio")
        assert counts.get(TC.PRE_ROTATION_CATEGORY, 0) == 0, counts          # tw_100001: running, no category yet
    src = (Path(__file__).resolve().parent / "browser.py").read_text()
    assert 'self.tw_pick["first_url_held_by_sibling"] = first' in src


def test_staging_on_the_data_volume():
    """t58 (supervisor / levi, after the N = 5 decode_stress hit the rootfs floor): a trio session's capture is staged
    on the data volume (data/_trio_staging) with every staged tcpdump as root (-Z root: the slots', the default class's
    and the DNS capture); exp.py fails a slot loudly when its capture is not growing once playing; the trio's raised
    floor applies to the data volume (orchestrate's preflight), the rootfs keeps single mode's."""
    assert T.STAGING.name == "_trio_staging" and T.STAGING.parent.name == "data"
    assert "TCPDUMP_Z=-Z root" in T.default_capture_vars(Path("/T"))
    import inspect
    assert "{TCPDUMP_Z}" in inspect.getsource(T.dns_capture_start)
    here = Path(__file__).resolve().parent
    exp_src, orch_src = (here / "exp.py").read_text(), (here / "orchestrate.py").read_text()
    assert 'TRIO["capture_growth"] = {**trio_slots.capture_growing(staging)' in exp_src
    assert 'raise RuntimeError(f"capture not growing in {staging}' in exp_src
    assert "preflight.run(_RUNS_DIR, max(MIN_FREE_GB, floor[\"floor_gb\"]))" in orch_src
    assert "trio_root_floor_gb" not in orch_src and "root_min_free_gb=floor" not in orch_src
    mk = (here / "Makefile").read_text()
    assert mk.count("exec tcpdump $(TCPDUMP_Z) -B $(TCPDUMP_B) -i") == 2 and "TCPDUMP_Z ?=" in mk
    import trio_slots as _T                       # t57 tw_100023 (levi): every capture's 64 MiB kernel buffer, one value
    assert f"TCPDUMP_B ?= {_T.TCPDUMP_B_KIB}" in mk and _T.TCPDUMP_B_KIB == 65536
    assert "-B {TCPDUMP_B_KIB}" in inspect.getsource(_T.dns_capture_start)


def main() -> int:
    test_slot_table()
    test_reservation_and_fullscreen_token()
    test_pick_matrix_reserved()
    test_run_at_n6()
    test_session_record()
    test_sibling_streams()
    test_yt_sibling_exclusion_and_long_not_live()
    test_exp_accepts_the_composition_tags()
    test_a_pick_is_claimed_before_it_lands()
    test_tt_idle_rule_reads_the_slots_own_ifb()
    test_mgmt_udp_port_is_the_makefiles_live_lookup()
    test_twitch_history_reads_both_roots_and_skips_uncategorised_running()
    test_staging_on_the_data_volume()
    print("test_nslot: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
