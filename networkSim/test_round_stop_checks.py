#!/usr/bin/env python3
"""ops/round_stop_checks.py (supervisor 2026-10-19): the chain stops only on the reader's gating failures (ok False),
the run's teardown, a session in _failed / missing from the reader, or device-record coverage < 0.95; a reported item
(4a's device_limited fps, an inventory reason with ok None) never stops it."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("round_stop_checks", HERE.parent / "ops" / "round_stop_checks.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def _sess(root: Path, name: str, covered: int = 100, secs: int = 100) -> Path:
    sd = root / "twitch" / name
    sd.mkdir(parents=True)
    hdr = {"header": True, "stamp": {"window": [999.0, 1000.0 + secs + 1]}}
    rows = [{"t": 1000.0 + i + 0.5, "gpu_busy_pct": {}} for i in range(covered)]
    (sd / "box_record.jsonl").write_text("".join(json.dumps(x) + "\n" for x in [hdr] + rows))
    return sd


def _row(name, **over):
    r = {"session": f"/d/twitch/{name}", "items1_2": {"1": {"ok": True, "reading": "PASS"},
                                                      "2": {"ok": None, "reading": "reported, excluded as in single mode"}},
         "item4a": {"ok": True, "device_limited_tiers": ["Q_720p"]}, "item5": {"ok": None}, "item7": {"ok": True},
         "item9": {"ok": True}, "item11": {"ok": True}, "item12": {"ok": True}, "item13": {"ok": True},
         "item14": {"ok": True}, "teardown": {"ok": True}}
    r.update(over)
    return r


def test_stop_rule():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        a, b = _sess(root, "tw_100005"), _sess(root, "tw_100006", covered=90)
        g = {"sessions": [_row("tw_100005"), _row("tw_100006")]}
        res = R.check(g, {"clean": True, "checks": {}}, [a])
        assert res["ok"] and "item4a device_limited ['Q_720p'] (reported)" in res["report"]["tw_100005"]["reported"], res
        assert res["report"]["tw_100005"]["device_record"]["covered_share"] == 1.0
        res = R.check(g, None, [a, b])                                     # 90 of 100 s covered: stop
        assert not res["ok"] and any("device record: 0.9" in x for x in res["gating"]), res
        g["sessions"][0]["item9"] = {"ok": False, "dropped_kernel_total": 3}
        g["sessions"][0]["items1_2"]["1"] = {"ok": False, "reading": "gates: chunk recall # vs logged responses"}
        res = R.check(g, {"clean": False, "checks": {"x": {"ok": False}}}, [a])
        assert len(res["gating"]) == 3 and res["gating"][0].startswith("run teardown"), res
        assert R.check({"sessions": []}, None, [a])["gating"] == ["tw_100005: not in the reader's output"]
        # levi: a session in _failed for inventory-only reasons (tw_100005's player_frozen) is reported, not a stop
        f = root / "_failed" / "twitch" / "tw_100007"
        f.parent.mkdir(parents=True)
        _sess(root, "tw_100007").rename(f)
        res = R.check({"sessions": [_row("tw_100007")]}, None, [f])
        assert res["ok"] and res["report"]["tw_100007"]["reported"][0].startswith("in _failed"), res
        bad2 = _row("tw_100007", items1_2={"2": {"ok": False, "reading": "gates: class:new_thing"}})
        assert not R.check({"sessions": [bad2]}, None, [f])["ok"]
        assert R.check({"sessions": []}, None, [root / "gone"])["gating"] == ["gone: session dir missing"]
        (a / "box_record.jsonl").unlink()
        assert any("no box_record" in x for x in R.check({"sessions": [_row("tw_100005")]}, None, [a])["gating"])
        (root / "g.json").write_text(json.dumps({"sessions": [_row("tw_100005")]}))
        assert R.main(["--gate2", str(root / "g.json"), str(a)]) == 1                 # no device record: BAD, exit 1


def test_systematic_rule():
    """levi: the same inventory kind on the same platform in 2 consecutive counted rounds stops; so does a platform's
    kind at >= 2 x its single-mode base rate with >= 3 events; below those, the per-session exclusion alone."""
    def g2(*sessions):
        return {"sessions": [_row(n, items1_2={"1": {"ok": True, "reading": "PASS"},
                                               "2": {"ok": None, "reasons": [], "session_class": cls}})
                             for n, cls in sessions]}
    base = {"check": {"twitch": {"n_sessions": 40, "coarse": {"class:player_frozen": {"sessions": 2, "rate": 0.05}}}},
            "verify": {}}
    r1 = R.round_kinds(g2(("tw_100005", "player_frozen"), ("tw_200001", "normal")))
    assert r1["kinds"] == {"twitch": {"check": {"class:player_frozen": ["tw_100005"]}}} and r1["n_sessions"] == {"twitch": 2}
    assert R.systematic(r1, [], base) == []                                         # one event, first round
    r2 = R.round_kinds(g2(("tw_100006", "player_frozen")))
    bad = R.systematic(r2, [{"round_kinds": r1}], base)
    assert len(bad) == 1 and "2 consecutive" in bad[0], bad
    r3 = R.round_kinds(g2(("tw_100007", "normal")))
    r4 = R.round_kinds(g2(("tw_100008", "player_frozen")))                          # 3 events over 5 sessions: 0.6 >= 0.1
    bad = R.systematic(r4, [{"round_kinds": r1}, {"round_kinds": r2}, {"round_kinds": r3}], base)
    assert any(">= 2 x" in x for x in bad) and not any("consecutive" in x for x in bad), bad
    import hashlib
    inv = {"base_rates": base, "base_rates_sha": hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()}
    assert R.load_base_rates(inv, inv["base_rates_sha"][:12]) == base
    for wrong in ("000000000000", None):
        try:
            R.load_base_rates(inv, wrong)
            raise AssertionError(wrong)
        except SystemExit:
            pass


def test_startup_failure_rule():
    """levi: a startup_failure is content, excluded (items 1 / 2 don't gate) unless its channel played normally before
    in either mode, or a sibling in the round also failed to start."""
    def sess(root, rel, cls, t, verdict="FAIL", **meta):
        d = root / rel
        d.mkdir(parents=True)
        (d / "check.json").write_text(json.dumps({"verdict": verdict, "session_class": cls}))
        (d / "meta.json").write_text(json.dumps({"started_at": t, **meta}))
        hdr = {"header": True, "stamp": {"window": [t - 1, t + 101]}}
        (d / "box_record.jsonl").write_text("".join(json.dumps(x) + "\n" for x in
                                                    [hdr] + [{"t": t + i + 0.5, "gpu_busy_pct": {}} for i in range(100)]))
        return d
    fail2 = lambda name: _row(name, items1_2={"1": {"ok": True, "reading": "PASS"},
                                              "2": {"ok": False, "reading": "gates: class:startup_failure"}})
    with tempfile.TemporaryDirectory() as d:
        data = Path(d) / "data"
        yt = sess(data, "_trio/_failed/youtube/yt_100007", "startup_failure", 1000.0, channel_handle="@FuboSports")
        tt = sess(data, "_trio/tiktok/ti_100007", "normal", 1000.0, verdict="PASS", channel_url="https://www.tiktok.com/@x/live")
        g = {"sessions": [fail2("yt_100007"), _row("ti_100007")]}
        res = R.check(g, None, [yt, tt])
        assert res["ok"] and res["report"]["yt_100007"]["reported"][1].startswith("startup_failure: content, excluded"), res
        sess(data, "youtube/yt_5", "normal", 500.0, verdict="PASS", channel_handle="@FuboSports")   # single mode played it
        res = R.check(g, None, [yt, tt])
        assert not res["ok"] and any("played before ['yt_5']" in x for x in res["gating"]), res
    with tempfile.TemporaryDirectory() as d:
        data = Path(d) / "data"
        yt = sess(data, "_trio/_failed/youtube/yt_100007", "startup_failure", 1000.0, channel_handle="@FuboSports")
        tw = sess(data, "_trio/_failed/twitch/tw_100007", "startup_failure", 1000.0, channel_url="https://www.twitch.tv/robcdee")
        res = R.check({"sessions": [fail2("yt_100007"), fail2("tw_100007")]}, None, [yt, tw])
        assert not res["ok"] and any("siblings failed ['tw_100007']" in x for x in res["gating"]), res
    with tempfile.TemporaryDirectory() as d:   # levi (tw_100016): a logged player error is mechanics, never content
        data = Path(d) / "data"
        tw = sess(data, "_trio/_failed/twitch/tw_100016", "startup_failure", 1000.0,
                  channel_url="https://www.twitch.tv/x", player_errors=[{"reason": "error_page"}])
        res = R.check({"sessions": [fail2("tw_100016")]}, None, [tw])
        assert not res["ok"] and any("player errors ['error_page']" in x for x in res["gating"]), res


def test_a_content_startup_failure_is_no_systematic_event():
    """supervisor 2026-10-19 (yt_100010 after yt_100007, one channel): a startup_failure read as content is left out of
    the round's kinds and counts (listed under content), and an earlier round's too, from its list or, for a round written
    before the list, re-read by the same rule; a gating one still counts, so two in a row stop."""
    def sess(root, rel, cls, t, verdict="FAIL", **meta):
        d = root / rel
        d.mkdir(parents=True)
        (d / "check.json").write_text(json.dumps({"verdict": verdict, "session_class": cls}))
        (d / "meta.json").write_text(json.dumps({"started_at": t, **meta}))
        hdr = {"header": True, "stamp": {"window": [t - 1, t + 101]}}
        (d / "box_record.jsonl").write_text("".join(json.dumps(x) + "\n" for x in
                                                    [hdr] + [{"t": t + i + 0.5, "gpu_busy_pct": {}} for i in range(100)]))
        return d
    sf = lambda name: _row(name, session=f"/d/youtube/{name}",
                           items1_2={"1": {"ok": True, "reading": "PASS"},
                                     "2": {"ok": False, "reasons": [], "session_class": "startup_failure",
                                           "reading": "gates: class:startup_failure"}})
    base = {"check": {"youtube": {"coarse": {"class:startup_failure": {"sessions": 1, "rate": 0.01}}}}, "verify": {}}
    with tempfile.TemporaryDirectory() as d:
        data = Path(d) / "data"
        a = sess(data, "_trio/_failed/youtube/yt_100007", "startup_failure", 1000.0, channel_handle="@FuboSports")
        b = sess(data, "_trio/_failed/youtube/yt_100010", "startup_failure", 3000.0, channel_handle="@FuboSports")
        old_r1 = {"round_kinds": R.round_kinds({"sessions": [sf("yt_100007")]})}    # written before the content list
        assert "content" in old_r1["round_kinds"] and old_r1["round_kinds"]["kinds"]
        del old_r1["round_kinds"]["content"]
        r1 = R.check({"sessions": [sf("yt_100007")]}, None, [a], [], base)
        assert r1["ok"] and r1["round_kinds"]["content"] == ["yt_100007"] and r1["round_kinds"]["kinds"] == {}, r1
        for hist in ([r1], [old_r1]):
            r2 = R.check({"sessions": [sf("yt_100010")]}, None, [b], hist, base)
            assert r2["ok"] and r2["round_kinds"]["content"] == ["yt_100010"], (hist, r2)
        sess(data, "_trio/youtube/yt_100008", "normal", 2000.0, verdict="PASS", channel_handle="@FuboSports")
        r2 = R.check({"sessions": [sf("yt_100010")]}, None, [b], [old_r1], base)   # it played (yt_100008): gates
        assert not r2["ok"] and any("played before ['yt_100008']" in x for x in r2["gating"]), r2
        assert r2["round_kinds"]["kinds"] == {"youtube": {"check": {"class:startup_failure": ["yt_100010"]}}}, r2
        assert not any("consecutive" in x for x in r2["gating"]), r2        # yt_100007 (before yt_100008) stays content


def test_verify_kinds_on_pass_sessions_are_reported_never_systematic():
    """supervisor 2026-10-19 (round 2): verify's 'paused while labelled playing' on check-PASS sessions in two rounds in
    a row is reported with its share per round, never a systematic stop; a check exclusion still is."""
    def g2(name, verify_reasons=(), cls="normal"):
        return {"sessions": [_row(name, items1_2={
            "1": {"ok": None, "verdict": "suspicious", "reasons": list(verify_reasons)} if verify_reasons
            else {"ok": True, "reading": "PASS"},
            "2": {"ok": True, "reading": "PASS"} if cls == "normal" else
            {"ok": None, "reasons": [], "session_class": cls}})]}
    base = {"verify": {"twitch": {"coarse": {"paused while labelled playing": {"sessions": 1, "rate": 0.01}}}},
            "check": {"twitch": {"coarse": {"class:player_frozen": {"sessions": 1, "rate": 0.01}}}}}
    flag = ["paused while labelled playing (share 0.12)"]
    r1, r2 = R.round_kinds(g2("tw_100010", flag)), R.round_kinds(g2("tw_100013", flag))
    assert r1["kinds"] == {} and r2["reported"]["kinds"]["twitch"]["verify"], r2
    assert list(r2["reported"]["share"]["twitch"]["verify"].values()) == [1.0], r2
    assert R.systematic(r2, [{"round_kinds": r1}], base) == []
    f1, f2 = R.round_kinds(g2("tw_100011", cls="player_frozen")), R.round_kinds(g2("tw_100014", cls="player_frozen"))
    assert any("2 consecutive" in x for x in R.systematic(f2, [{"round_kinds": f1}], base))
    # levi on 6584cef: verify's mechanics kinds (chunk recall, flow attribution) still count; a content band does not
    for mech in ("chunk recall 3 vs logged responses >= 10 kB", "selected flows carry 0.62 of downstream"):
        m1, m2 = R.round_kinds(g2("tw_100015", [mech])), R.round_kinds(g2("tw_100016", [mech]))
        assert m2["kinds"]["twitch"]["verify"] and not m2["reported"]["kinds"], m2
        assert any("2 consecutive" in x for x in R.systematic(m2, [{"round_kinds": m1}], base)), mech
    band = R.round_kinds(g2("tw_100017", ["B/s per tier 1200 below band [800, 950]"]))
    assert band["kinds"] == {} and band["reported"]["kinds"]["twitch"]["verify"], band


if __name__ == "__main__":
    test_verify_kinds_on_pass_sessions_are_reported_never_systematic()
    test_stop_rule()
    test_startup_failure_rule()
    test_systematic_rule()
    test_a_content_startup_failure_is_no_systematic_event()
    print("test_round_stop_checks: ok")
