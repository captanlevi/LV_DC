#!/usr/bin/env python3
"""gate2_items (t53 gate 2 items 4a / 8, levi's checklist): playing intervals, the shot-free dropped-frame rate (+- 2 s
of every shot's [t_decide, ts]), counter resets, visibility at every tick, rendered fps per tier against the rendition's
fps (>= 0.9 x), and sessions from before the new columns. Synthetic labels.csv / shots/index.jsonl.

    python3 test_gate2_items.py        (exit 0 = pass)
"""
import csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate2_items as G  # noqa: E402

COLS = ["ts", "type", "video_w", "video_h", "dropped", "current_time", "paused", "is_ad", "visibility", "total_frames"]


def session(d, n=60, fps=30.0, drop_every=1, vis=None, shots=(), reset_at=None, paused_at=(), cols=COLS, h=720):
    sd = Path(d)
    (sd / "shots").mkdir(parents=True, exist_ok=True)
    rows, dropped, total = [], 0, 0
    for i in range(n):
        if reset_at is not None and i == reset_at:
            dropped, total = 0, 0                                     # a new <video> element
        elif i:
            dropped += drop_every
            total += fps
        r = {"ts": (1000.0 + i) * 1000, "type": "res_meta", "video_w": h * 16 // 9, "video_h": h, "dropped": dropped,
             "current_time": float(i), "paused": "True" if i in paused_at else "False", "is_ad": "False",
             "visibility": (vis or {}).get(i, "visible"), "total_frames": total}
        rows.append([r[c] for c in cols])
    with open(sd / "labels.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    (sd / "shots" / "index.jsonl").write_text("".join(json.dumps({"t_decide": 1000.0 + s, "ts": 1000.0 + s + 0.4}) + "\n"
                                                      for s in shots))
    return sd


def test_item8_shot_free_rate():
    with tempfile.TemporaryDirectory() as d:
        r = G.session_items(session(d, shots=(30,)))
        i8 = r["item8"]
        # 59 one-second intervals; the shot at [30, 30.4] +- 2 s touches intervals [28,29] .. [32,33] (strictly overlapping)
        assert i8["playing_s_all"] == 59.0 and i8["dropped_all"] == 59 and i8["dropped_per_playing_s_all"] == 1.0, i8
        assert i8["playing_s_shot_free"] == 54.0 and i8["dropped_shot_free"] == 54 and i8["shots"] == 1, i8
    with tempfile.TemporaryDirectory() as d:                          # a counter reset is a step of 0, never negative
        i8 = G.session_items(session(d, reset_at=20))["item8"]
        assert i8["dropped_all"] == 58 and i8["playing_s_all"] == 59.0, i8
    with tempfile.TemporaryDirectory() as d:                          # paused ticks bound no playing interval
        i8 = G.session_items(session(d, paused_at=(10, 11)))["item8"]
        assert i8["playing_s_all"] == 56.0, i8


def _d(fps):
    return {"Q_720p": {"delivered_fps": fps}}


def test_item4a_visibility_and_fps():
    """Decision A (levi): 4a gates visibility only; rendered / delivered below 0.9 stamps the tier device_limited,
    reported (ok None), never a FAIL."""
    with tempfile.TemporaryDirectory() as d:
        a = G.session_items(session(d), _d(30.0))["item4a"]
        assert a["visibility"] == {"recorded": True, "samples": 60, "not_visible": 0, "values": {"visible": 60},
                                   "first_not_visible_t": None, "ok": True}, a
        f = a["rendered_fps"]["Q_720p"]
        assert (f["fps_per_media_s_shot_free"], f["delivered_fps"], f["ratio"], f["device_limited"], f["ok"], a["ok"]) \
            == (30.0, 30.0, 1.0, False, None, True), a
        assert (f["playback_rate_shot_free"], f["dropped_share_shot_free"]) == (1.0, round(1 / 30, 3)), f
        assert a["fps60_covered"] is False
    with tempfile.TemporaryDirectory() as d:                          # one hidden tick fails 4a
        a = G.session_items(session(d, vis={7: "hidden"}), _d(30.0))["item4a"]
        assert a["visibility"]["not_visible"] == 1 and a["visibility"]["first_not_visible_t"] == 1007.0 and a["ok"] is False
    with tempfile.TemporaryDirectory() as d:                          # 20 frames per media s of a 60 delivered: < 0.9 x
        a = G.session_items(session(d, fps=20.0), _d(60.0))["item4a"]
        f = a["rendered_fps"]["Q_720p"]
        assert f["device_limited"] is True and f["ok"] is None and a["device_limited_tiers"] == ["Q_720p"], a
        assert a["ok"] is True and a["fps60_covered"] is True, a                 # visibility alone gates
        a = G.session_items(session(d, fps=26.0))["item4a"]           # no delivered fps: reported, not a FAIL
        f = a["rendered_fps"]["Q_720p"]
        assert f["device_limited"] is None and f["note"].startswith("no delivered fps") and a["ok"] is True, a
    with tempfile.TemporaryDirectory() as d:                          # 9 playing seconds: insufficient support, not read
        a = G.session_items(session(d, n=10, fps=20.0), _d(60.0))["item4a"]
        assert a["rendered_fps"]["Q_720p"]["note"] == "insufficient support" and a["ok"] is True, a
        assert a["fps60_covered"] is False
    with tempfile.TemporaryDirectory() as d:                          # a session from before the columns
        r = G.session_items(session(d, cols=COLS[:-2]))                # not recorded: a FAIL
        assert r["item4a"]["visibility"] == {"recorded": False, "ok": False} and r["item4a"]["ok"] is False, r
        assert r["item4a"]["rendered_fps"] == {"recorded": False} and r["item4a"]["fps60_covered"] is None, r
        assert r["item8"]["playing_s_all"] == 59.0


def _tc(sd, seconds, rate=400.0, ratio=1.0, backlog=True, state="3", t0=1000.0):
    rows, b = [], 0
    for i in range(seconds + 1):
        rows.append({"ts": t0 + i, "in": {"rate_kbit": rate, "bytes": b, "drops": 0, "backlog": 1500 if backlog else 0}})
        b += int(rate * ratio * 1000 / 8)
    (Path(sd) / "tc_stats.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (Path(sd) / "net_labels.txt").write_text(json.dumps({"timestamp": t0 - 1, "state": state, "speed": rate}) + "\n")


def test_item5_byte_total_ratio():
    """levi (gate-2 call on pilot-1): per state over shaper-bound seconds, sum(bytes) x 8 / sum(rate x s) against
    [0.9, 1.15]; since round 1 reported only (ok None everywhere, the readings and within_band kept)."""
    for ratio, secs, backlog, want in ((1.0, 30, True, ("within the band", True)), (0.8, 30, True, ("below the band", False)),
                                       (1.3, 30, True, ("above the band", False)), (1.0, 5, True, ("not shaper-bound", None)),
                                       (0.5, 30, False, (None, None))):
        with tempfile.TemporaryDirectory() as d:
            _tc(d, secs, ratio=ratio, backlog=backlog)
            r = G.item5(Path(d))
            if want[0] is None:                                         # never backlogged: nothing read
                assert r["per_state"] == {} and r["ok"] is None, r
                continue
            s = r["per_state"]["3"]
            assert (s["reading"], s.get("within_band")) == want and s["ratio"] == round(ratio, 3), (ratio, r)
            assert s["ok"] is None and r["ok"] is None and "never gating" in r["role"], r
    with tempfile.TemporaryDirectory() as d:                          # 0.5 s samples: 30 intervals are 15 s, read
        rows, b = [], 0
        for i in range(31):
            rows.append({"ts": 1000.0 + i * 0.5, "in": {"rate_kbit": 400.0, "bytes": b, "drops": 0, "backlog": 1500}})
            b += int(400.0 * 1000 / 8 * 0.5)
        (Path(d) / "tc_stats.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (Path(d) / "net_labels.txt").write_text(json.dumps({"timestamp": 999.0, "state": "3", "speed": 400.0}) + "\n")
        s = G.item5(Path(d))["per_state"]["3"]
        assert (s["bound_s"], s["ratio"], s["reading"]) == (15.0, 1.0, "within the band"), s
    with tempfile.TemporaryDirectory() as d:                          # a failed class read: the rate only, skipped
        _tc(d, 30)
        rows = (Path(d) / "tc_stats.jsonl").read_text().splitlines()
        rows[10] = json.dumps({"ts": json.loads(rows[10])["ts"], "in": {"rate_kbit": 400.0}})
        (Path(d) / "tc_stats.jsonl").write_text("\n".join(rows) + "\n")
        r = G.item5(Path(d))
        assert r["samples_partial_skipped"] == 1 and r["per_state"]["3"]["reading"] == "within the band", r
        for i in range(2, 30, 3):                                       # more than 0.1 partial: incomplete, a FAIL
            rows[i] = json.dumps({"ts": json.loads(rows[i])["ts"], "in": {"rate_kbit": 400.0}})
        (Path(d) / "tc_stats.jsonl").write_text("\n".join(rows) + "\n")
        r = G.item5(Path(d))
        assert r["ok"] is None and r["reading"] == "tc recording incomplete", r
    with tempfile.TemporaryDirectory() as d:
        assert G.item5(Path(d)) == {"recorded": False, "role": G.ITEM5_ROLE, "ok": None}


def test_items1_2_content_bands():
    """levi: a non-PASS whose every reason is a content band is reported (excluded as in single mode); else it gates."""
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        (sd / "verify.json").write_text(json.dumps({"verdict": "suspicious", "reasons": ["B/s outside old p10..p90 for ['360p']"]}))
        (sd / "check.json").write_text(json.dumps({"verdict": "PASS", "session_class": "normal", "reasons": []}))
        r = G.items1_2(sd)
        assert (r["1"]["ok"], r["1"]["reading"].startswith("content band only"), r["2"]["ok"]) == (None, True, True), r
        (sd / "verify.json").write_text(json.dumps({"verdict": "FAIL", "reasons": ["B/s outside old p10..p90 for ['360p']",
                                                                                   "chunk recall 0.40 vs logged responses"]}))
        (sd / "check.json").write_text(json.dumps({"verdict": "PASS", "session_class": "player_frozen", "reasons": []}))
        r = G.items1_2(sd)
        assert r["1"]["ok"] is False and r["2"]["ok"] is False, r


def test_items1_2_inventory():
    """Q am. 16 (levi): with brad's inventory, a non-PASS whose every coarse kind single mode produced is reported
    (player_frozen as the class pseudo-reason; a tier list blanked), a new coarse kind gates and is named; the file is
    refused unless its kinds hash to its sha with the expected prefix. kind / sha parity with brad's examples."""
    assert G.reason_kind("chunk recall 0.40 vs logged responses") == "chunk recall # vs logged responses"
    assert G.reason_kind("B/s outside old p10..p90 for ['360p', '480p']", coarse=True) == "B/s outside old p#..p# for [...]"
    kinds = {"verify": {"coarse": {"B/s outside old p#..p# for [...]": 3}, "fine": {"B/s outside old p#..p# for ['#p']": {}}},
             "check": {"coarse": {"class:player_frozen": 2}, "fine": {"class:player_frozen": {}}}}
    inv = {"inventory_sha": G.inventory_sha(kinds), "kinds": kinds}
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        (Path(d) / "inv.json").write_text(json.dumps(inv))
        loaded = G.load_inventory(Path(d) / "inv.json", inv["inventory_sha"][:12])
        for bad in ("000000000000", None):
            try:
                G.load_inventory(Path(d) / "inv.json", bad)
                raise AssertionError(bad)
            except SystemExit:
                pass
        (sd / "verify.json").write_text(json.dumps({"verdict": "suspicious", "reasons": ["B/s outside old p10..p90 for ['1080p']"]}))
        (sd / "check.json").write_text(json.dumps({"verdict": "PASS", "session_class": "player_frozen", "reasons": []}))
        r = G.items1_2(sd, loaded)
        assert (r["1"]["ok"], r["2"]["ok"]) == (None, None) and r["2"]["new_coarse_kinds"] == [], r
        assert r["1"]["new_fine_kinds"] == ["B/s outside old p#..p# for ['#p']"] or r["1"]["new_fine_kinds"] == [], r
        (sd / "verify.json").write_text(json.dumps({"verdict": "FAIL", "reasons": ["chunk recall 0.40 vs logged responses"]}))
        (sd / "check.json").write_text(json.dumps({"verdict": "PASS", "session_class": "normal", "reasons": []}))
        r = G.items1_2(sd, loaded)
        assert r["1"]["ok"] is False and r["1"]["new_coarse_kinds"] == ["chunk recall # vs logged responses"] and r["2"]["ok"]


def test_fullscreen_report():
    """levi (t53 (a)): per fullscreen yt session the keeper's counts and the cadence shots' CDP-fallback share, flagged
    above 0.2."""
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        (sd / "shots").mkdir()
        rows = ([{"reason": "cadence", "capture": "x11", "on_top": True}] * 7
                + [{"reason": "cadence", "capture": "cdp_fallback", "on_top": False}] * 3 + [{"reason": "tier_change"}])
        (sd / "shots" / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        meta = {"fullscreen_keeper": {"checks": 900, "not_on_top": 4, "events": [{"activate_sent": True}] * 4}}
        r = G.fullscreen_report(sd, meta)
        assert r == {"checks": 900, "not_on_top": 4, "raises": 4, "cadence_shots": 10, "cdp_fallback": 3,
                     "cdp_fallback_share": 0.3, "off_top_shots": 3, "shot_readings_flag": True}, r
        meta["fullscreen_keeper"]["raises"] = 250                # the keeper's full count wins over the bounded list
        assert G.fullscreen_report(sd, meta)["raises"] == 250
        assert G.fullscreen_report(sd, {}) is None


def test_concurrency_and_overlap_items():
    """Items 7 / 9 / teardown from meta.concurrency (pilot-1's tw_100003 shape: slot_down's only failures re-delete what
    slot_detach removed) and 11-14 from flow_overlap's run json (levi's call: mDNS reported when packet-disjoint)."""
    det = [{"cmd": "tc filter del dev wlp0s20f3 parent ffff: prio 11", "rc": 0}]
    nf = "Error: Filter with specified priority/protocol not found."
    meta = {"concurrency": {"mode": "trio", "tc": {"flagged_share": 0.0},
                            "capture_drops": {"dropped_kernel_total": 0}, "slot_detach": det, "staging_left": [],
                            "slot_down": {"failed": [{"cmd": det[0]["cmd"], "err": nf}]}}}
    r = G.concurrency_items(meta)
    assert (r["item7"]["ok"], r["item9"]["ok"], r["teardown"]["ok"]) == (True, True, True), r
    meta["concurrency"]["slot_down"]["failed"].append({"cmd": "ip link del lvi_tw", "err": "busy"})
    meta["concurrency"]["capture_drops"]["dropped_kernel_total"] = 3
    r = G.concurrency_items(meta)
    assert (r["item9"]["ok"], r["teardown"]["ok"]) == (False, False), r
    assert G.concurrency_items({"concurrency": {"mode": "single"}}) == {} and G.concurrency_items({}) == {}
    mc = {"flow": ["udp", ["192.168.4.50", 5353], ["224.0.0.251", 5353]], "packets_a": 3, "packets_b": 4, "packets_in_both": 0}
    fo = {"pairs": [{"a": "tiktok/ti_1", "b": "twitch/tw_1", "shared_flows": 1, "shared_unicast_flows": 0,
                     "shared_link_local_multicast": [mc], "shared_quic_cids": 0}],
          "sessions": {"twitch/tw_1": {"sibling_family_snis": {}, "default_media_snis_in_window": [],
                                       "default_udp_from_media_addresses": {"packets": 0}, "flows_also_in_default": 1,
                                       "dns_answers_json": {"present": True, "media_unnamed": []}}},
          "split_detail": {"twitch/tw_1": {"rows": [{"payload_packets": 0}]}}}
    r = G.overlap_items("twitch/tw_1", fo)
    assert [r[k]["ok"] for k in ("item11", "item12", "item13", "item14")] == [True, True, True, True], r
    assert (r["item11"]["link_local_multicast_flows"], r["item11"]["default_split_flows_control_only"]) == (1, 1), r
    fo["pairs"][0]["shared_link_local_multicast"][0]["packets_in_both"] = 1        # one packet in two pcaps: gates
    assert G.overlap_items("twitch/tw_1", fo)["item11"]["ok"] is False
    fo["pairs"][0]["shared_link_local_multicast"][0]["packets_in_both"] = 0
    fo["split_detail"]["twitch/tw_1"]["rows"].append({"payload_packets": 2})       # a payload-carrying split: gates
    assert G.overlap_items("twitch/tw_1", fo)["item11"]["ok"] is False
    fo["split_detail"]["twitch/tw_1"]["rows"].pop()
    # levi (round 1): a link-local multicast split (avahi's mDNS) is reported, not counted, when packet-disjoint
    av = {"flow": ["udp", ["192.168.4.50", 5353], ["224.0.0.251", 5353]], "payload_packets": 1, "default_packets": 1,
          "class": "after_idle"}
    fo["split_detail"]["twitch/tw_1"]["rows"].append(dict(av, link_local_multicast=True, packets_in_both=0))
    r = G.overlap_items("twitch/tw_1", fo)["item11"]
    assert r["ok"] is True and r["default_split_flows_payload"] == 0, r
    assert r["default_split_link_local_multicast"][0]["disjoint"] is True, r
    fo["split_detail"]["twitch/tw_1"]["rows"][-1]["packets_in_both"] = 1                 # the same packet: counted, a FAIL
    assert G.overlap_items("twitch/tw_1", fo)["item11"]["ok"] is False
    fo["split_detail"]["twitch/tw_1"]["rows"][-1] = dict(av)                              # an older output, no field:
    r = G.overlap_items("twitch/tw_1", fo)["item11"]                                      # inside the span, undecided
    assert r["ok"] is False and r["default_split_link_local_multicast"][0]["disjoint"] is None and "note_multicast" in r, r
    fo["split_detail"]["twitch/tw_1"]["rows"][-1] = dict(av, **{"class": "after_last"})   # outside the span: disjoint
    assert G.overlap_items("twitch/tw_1", fo)["item11"]["ok"] is True
    del fo["split_detail"]                                                          # no --detail: not recorded, a FAIL
    r = G.overlap_items("twitch/tw_1", fo)["item11"]
    assert r["ok"] is False and r["recorded"] is False and "--detail" in r["note"], r
    m = G.concurrency_items({"concurrency": {"mode": "trio"}})                       # fields missing: FAIL, not "not gated"
    assert (m["item7"]["ok"], m["item7"]["recorded"], m["item9"]["ok"], m["item9"]["recorded"]) == (False, False, False, False), m
    fo["sessions"]["twitch/tw_1"].pop("dns_answers_json")
    assert G.overlap_items("twitch/tw_1", dict(fo, split_detail={"twitch/tw_1": {"rows": []}}))["item14"]["ok"] is False
    assert all(v["ok"] is False for v in G.missing_overlap_items().values())
    with tempfile.TemporaryDirectory() as d:                          # a trio session without a flow_overlap record
        sd = session(d)
        (sd / "meta.json").write_text(json.dumps({"concurrency": {"mode": "trio"}}))
        r = G.session_items(sd)
        assert all(r[k]["ok"] is False and r[k]["recorded"] is False for k in ("item7", "item9", "item11", "item14")), r
        assert r["record"] == {"flag": None, "era": None, "is_record": False}, r["record"]
    assert G.overlap_items("youtube/yt_9", fo) == {} and G.overlap_items("twitch/tw_1", None) == {}


def test_cli():
    with tempfile.TemporaryDirectory() as d:
        sd = session(Path(d) / "tw_100001", shots=(30,))
        (Path(d) / "fps.json").write_text(json.dumps({"tw_100001": {"detail": {"Q_720p": {"delivered_fps": 60.0}}}}))
        G.main([str(sd), "--rendition-fps", str(Path(d) / "fps.json"), "--out", str(Path(d) / "o.json")])
        r = json.loads((Path(d) / "o.json").read_text())["sessions"][0]
        q = r["item4a"]["rendered_fps"]["Q_720p"]
        assert q["delivered_fps"] == 60.0 and q["device_limited"] is True and r["item4a"]["ok"] is True, r
        assert r["items1_2"]["1"]["reading"] == "verify.json missing" and r["item5"]["ok"] is None


def main() -> int:
    test_item8_shot_free_rate()
    test_item4a_visibility_and_fps()
    test_item5_byte_total_ratio()
    test_items1_2_content_bands()
    test_items1_2_inventory()
    test_fullscreen_report()
    test_concurrency_and_overlap_items()
    test_cli()
    print("test_gate2_items: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
