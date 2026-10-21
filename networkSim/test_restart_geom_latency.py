#!/usr/bin/env python3
"""Restart §7 (the per-session geometry draw) and §8 (twitch's low-latency setting), levi's prereg 2026-10-21:
geometry.draw_geometry, tw_latency.py, their wiring in orchestrate._trio_refill / browser.py / exp.py (the coverage
rule's uncoupled record: test_coverage). No browser: a fake CDP page and the trio harness of test_trio_slots.

    python3 test_restart_geom_latency.py        (exit 0 = pass)
"""
import collections
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import geometry as G  # noqa: E402
import orchestrate as O  # noqa: E402
import tw_latency as L  # noqa: E402
import test_trio_slots as TS  # noqa: E402  (its run_trio harness)


def test_geometry_draw():
    yt = G.draw_geometry("youtube", "s")
    assert yt["support"] == G.GEOMETRY_CHOICES["youtube"] and "fullscreen:1920x1080" in yt["support"]
    assert "theater:1920x1080" in yt["support"], yt                                 # prereg d32a48ce: fullscreen 1/3
    assert abs(yt["weights"]["fullscreen:1920x1080"] - 1 / 3) < 1e-6 and all(
        abs(v - 1 / 6) < 1e-6 for g, v in yt["weights"].items() if not g.startswith("fullscreen")), yt
    nofs = G.draw_geometry("youtube", "s", ("fullscreen",))                        # excluded: the other four, equal
    assert set(nofs["weights"].values()) == {0.25}, nofs
    assert G.draw_geometry("youtube", "s") == yt                                   # seeded: the same seed, the same draw
    assert G.draw_geometry("twitch", "s")["support"] == G.GEOMETRY_CHOICES["twitch"]
    assert G.draw_geometry("youtube", "s", ("fullscreen",))["support"] == G.GEOMETRY_CHOICES["youtube"][:4]
    assert G.draw_geometry("nope", "s") is None
    n = collections.Counter(G.draw_geometry("youtube", f"r:{i}")["geometry"] for i in range(5000))
    assert set(n) == set(G.GEOMETRY_CHOICES["youtube"]) and 1550 < n["fullscreen:1920x1080"] < 1780 and all(
        750 < v < 920 for g, v in n.items() if not g.startswith("fullscreen")), n   # 1/3 and 1/6 each
    # the one-fullscreen token: a fullscreen draw is redrawn among the others by the same generator, recorded
    held = [G.draw_geometry("youtube", f"r:{i}", token_held=True) for i in range(500)]
    assert not any(r["geometry"].startswith("fullscreen") for r in held)
    fs = [r for r in held if r["drawn"].startswith("fullscreen")]
    assert fs and all(r["token_deferred"] == "fullscreen:1920x1080" and r["token_held"] for r in fs)
    assert all("token_deferred" not in r and r["geometry"] == r["drawn"] for r in held if r not in fs)
    assert all(G.draw_geometry("youtube", f"r:{i}")["drawn"] == held[i]["drawn"] for i in range(500))   # same first draw


def _run(slots, targets, draw=None, fullscreen=True):
    spawned = []

    def spawn(cmd, env, log):
        spawned.append((cmd, env))
        return TS.FakeProc(1, 0, [])
    saved = G.draw_geometry, O.TRIO_YT_FULLSCREEN
    O.TRIO_YT_FULLSCREEN = fullscreen
    if draw:
        G.draw_geometry = draw
    try:
        with tempfile.TemporaryDirectory() as d:
            rc, calls = TS._trio(Path(d), targets, spawn, slots=slots)
    finally:
        G.draw_geometry, O.TRIO_YT_FULLSCREEN = saved
    assert rc == 0, rc
    return spawned


def test_trio_wiring():
    # every session's geometry is its own draw from its seed; the twitch slot also gets its low-latency draw
    sp = _run({"youtube": 1, "twitch": 1, "tiktok": 1}, {"youtube": 5, "twitch": 5, "tiktok": 5})
    for cmd, env in sp:
        p, tag = cmd[cmd.index("--platform") + 1], cmd[cmd.index("--slot") + 1]
        gd = json.loads(env["LVDC_GEOMETRY_DRAW"])
        assert gd["seed"].startswith(f"{O.SESSION_DRAW_SEED}:") and gd["seed"].endswith(f":{tag}:1"), gd
        # token_held is the run's state (yt1's own draw holds the token with p 0.2 from the run id), so it is passed
        assert gd == G.draw_geometry(p, gd["seed"], () if p == "youtube" else ("fullscreen",), gd["token_held"])
        assert cmd[cmd.index("--geometry") + 1] == gd["geometry"]
        if p == "twitch":
            ll = json.loads(env["LVDC_TW_LOW_LATENCY"])
            assert ll == L.draw(gd["seed"]) and cmd[cmd.index("--tw-low-latency") + 1] == ll["drawn"], (ll, cmd)
        else:
            assert "--tw-low-latency" not in cmd and "LVDC_TW_LOW_LATENCY" not in env
    # the token in the trio: yt1 draws fullscreen and holds it, yt2's draw sees it held and is redrawn
    calls = []

    def always_fs(p, seed, excl=(), held=False):
        calls.append((seed.rsplit(":", 2)[1], held))
        r = saved_draw(p, "x", excl, held)
        return {**r, "drawn": "fullscreen:1920x1080", "geometry": r["geometry"] if held else "fullscreen:1920x1080"}
    saved_draw = G.draw_geometry
    sp = _run({"youtube": 2, "twitch": 1}, {"youtube": 5, "twitch": 5}, draw=always_fs)
    by = {cmd[cmd.index("--slot") + 1]: cmd for cmd, _ in sp}
    assert by["yt1"][by["yt1"].index("--geometry") + 1] == "fullscreen:1920x1080"
    assert not by["yt2"][by["yt2"].index("--geometry") + 1].startswith("fullscreen")
    assert ("yt1", False) in calls and ("yt2", True) in calls, calls


class FakePage:
    def __init__(self, reads=(), fail_eval=False, hook="7"):
        self.calls, self.reads, self.fail_eval, self.hook = [], list(reads), fail_eval, hook

    def call(self, method, params=None):
        self.calls.append(method)
        return {"identifier": self.hook} if method == "Page.addScriptToEvaluateOnNewDocument" and self.hook else {}

    def eval(self, js, await_promise=False):
        if self.fail_eval:
            raise RuntimeError("socket closed")
        return json.dumps(self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]) if self.reads else "null"


def test_latency_draw_and_apply():
    a = L.draw("seed-1")
    assert a == L.draw("seed-1") and a["drawn"] in ("on", "off") and a["p_on"] == 0.5 and a["seed"] == "seed-1"
    n = collections.Counter(L.draw(f"s{i}")["drawn"] for i in range(4000))
    assert 1850 < n["on"] < 2150, n
    assert "localStorage.setItem(\"lowLatencyModeEnabled\", \"false\")" in L.set_js(False)
    assert "\"true\"" in L.set_js(True) and "twitch\\.tv$" in L.set_js(True)
    assert L.verdict("on", {"mode": "Low Latency"}) is True and L.verdict("off", {"mode": "Normal Latency"}) is True
    assert L.verdict("on", {"mode": "Normal Latency"}) is False and L.verdict("off", {"mode": None}) is None
    assert L.verdict("on", None) is None
    pg = FakePage()
    rec = L.apply(pg, "off")
    assert pg.calls == ["Page.enable", "Page.addScriptToEvaluateOnNewDocument"] and rec["applied"] and rec["hook"] == "7"
    assert L.apply(FakePage(hook=None), "on")["applied"] is False


def test_latency_read_back():
    nap = []
    ok = L.read_back(FakePage([{"mode": None}, {"mode": "Normal Latency", "buffer": "5.47 sec.",
                                                "latency": "6.08 sec.", "local_storage": "false"}]),
                     {"drawn": "off", "hook": "7"}, sleep=nap.append)
    assert ok["ok"] is True and ok["local_storage_ok"] and ok["buffer_s"] == 5.47 and ok["latency_s"] == 6.08, ok
    assert ok["hook_removed"] is True and len(nap) == 1
    pg = FakePage([{"mode": "Low Latency", "local_storage": "true"}])
    bad = L.read_back(pg, {"drawn": "off", "hook": "7"}, sleep=nap.append)   # a failed set: recorded, no raise
    assert bad["ok"] is False and bad["local_storage_ok"] is False and bad["read_back"]["mode"] == "Low Latency"
    assert pg.calls == ["Page.removeScriptToEvaluateOnNewDocument", "Page.disable"]
    nap.clear()
    none = L.read_back(FakePage([{"mode": None, "rows": 0}]), {"drawn": "on", "hook": "7"}, sleep=nap.append)
    assert none["ok"] is None and len(nap) == L.READ_TRIES - 1                 # unreadable: None, after every try
    err = L.read_back(FakePage(fail_eval=True), {"drawn": "on", "hook": "7"}, sleep=lambda s: None)
    assert err["ok"] is None and "socket closed" in err["read_back"]["error"]


def test_browser_and_exp_wiring():
    b = (HERE / "browser.py").read_text()
    i_apply = b.index("self.tw_latency = tw_latency.apply(self._page, self.tw_low_latency)")
    assert i_apply < b.index("self._master = MasterListener(self._ws_url).start()") < b.index(
        "self.list_url = _navigate_to_live(self._page, self.platform, first_url=first,")   # before any twitch document
    i_read = b.index("self.tw_latency = tw_latency.read_back(self._page, self.tw_latency)")
    assert b.index("self.video_stats = self._page.eval(_TWITCH_VIDEO_STATS_JS, await_promise=True)") < i_read
    for i in (i_apply, i_read):                  # never an abort: each inside its own try
        assert b.rfind("try:", 0, i) > b.rfind("\n            if self.tw_l", 0, i), i
    e = (HERE / "exp.py").read_text()
    assert '(("LVDC_GEOMETRY_DRAW", "geometry_draw"), ("LVDC_TW_LOW_LATENCY", "tw_low_latency"))' in e
    assert 'tw_low_latency=args.tw_low_latency if platform == "twitch" else None)' in e
    assert 'meta_extra["tw_low_latency"] = {**ll_draw, **browser.tw_latency}' in e
    assert '"player_device_px": dev(g.get("player_client")),' in e and '"viewport_device_px": dev(g.get("viewport"))}' in e


def main() -> int:
    test_geometry_draw()
    test_trio_wiring()
    test_latency_draw_and_apply()
    test_latency_read_back()
    test_browser_and_exp_wiring()
    print("test_restart_geom_latency: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
