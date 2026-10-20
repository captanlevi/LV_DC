#!/usr/bin/env python3
"""t56 decode_stress (ops/diag/decode_stress.py) and its plumbing: the stress root (LVDC_TRIO_DIR=_trio_stress, its own
name floor, nothing else accepted), orchestrate --trio-stress holding a pinned state (exp.py --hold-state), the stress's
commands (every slot pinned and held, one fullscreen yt slot, never --trio-record), the readings (stall cause by
buffer-ahead at the event, playing seconds, rendered fps / dropped share per tier, tt's 60 fps tier stated) and the gate
(device stalls per slot, unreadable slots a stated gap, swap / OOM, CPU headroom, GPU reported)."""
import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("decode_stress", HERE.parent / "ops" / "diag" / "decode_stress.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

COLS = ["ts", "type", "video_h", "buffered_ahead_s", "paused", "playhead_still_s", "is_ad", "total_frames", "dropped"]


def _labels(path: Path, rows: list[list]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        w.writerows(rows)


def _ticks(t0: float, n: int, h: int, fps: float, frames0: float = 0.0, dropped_per_s: float = 0.0, paused=False):
    return [[str((t0 + i) * 1000), "res_meta", str(h), "5.0", str(paused), "0", "False", str(frames0 + fps * i),
             str(dropped_per_s * i)] for i in range(n)]


def test_stress_root():
    """LVDC_TRIO_DIR selects the stress root and its name floor; any other value stops the import."""
    def probe(val):
        env = {**os.environ, "LVDC_TRIO_DIR": val} if val is not None else {k: v for k, v in os.environ.items()
                                                                           if k != "LVDC_TRIO_DIR"}
        return subprocess.run([sys.executable, "-c", "import trio_slots as T; print(T.TRIO_DIR, T.TRIO_NAME_FLOOR)"],
                              cwd=HERE, env=env, capture_output=True, text=True)
    assert probe(None).stdout.split() == ["_trio", "100000"]
    assert probe("_trio_stress").stdout.split() == ["_trio_stress", "900000"]
    r = probe("_elsewhere")
    assert r.returncode != 0 and "only _trio or _trio_stress" in r.stderr, r.stderr


def test_trio_stress_holds_pinned_state():
    """orchestrate --trio-stress: a slot's pinned state goes to exp.py as --hold-state; without it, never."""
    import orchestrate as O
    import test_trio_slots as TT
    for on in (False, True):
        with tempfile.TemporaryDirectory() as d:
            spawned = []

            def spawn(cmd, env, log):
                spawned.append(cmd)
                return TT.FakeProc(1, 0, [])
            saved = dict(O.TRIO_PINS), O.TRIO_STRESS
            O.TRIO_PINS.clear()
            O.TRIO_PINS.update({"tw1": {"state": "10", "reason": "decode_stress"}})
            O.TRIO_STRESS = on
            try:
                rc, _ = TT._trio(Path(d), {"twitch": 5}, spawn)
            finally:
                O.TRIO_PINS.clear()
                O.TRIO_PINS.update(saved[0])
                O.TRIO_STRESS = saved[1]
            c = spawned[0]
            assert rc == 0 and c[c.index("--initial-state") + 1] == "10", c
            assert (("--hold-state" in c) and c[c.index("--hold-state") + 1] == "10") is on, (on, c)


def test_commands():
    a = SimpleNamespace(state="10", yt_fullscreen=True, dpms_off=True, layout="composite", iface="wlp0s20f3",
                        python="/v/python")
    box, cmd = D.commands(a, {"youtube": 2, "twitch": 1, "tiktok": 1}, ["yt1", "yt2", "tw1", "tt1"], Path("/o"))
    pins = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--trio-pin"]
    assert pins == ["yt1:state=10,geometry=fullscreen:1920x1080", "yt2:state=10", "tw1:state=10", "tt1:state=10"], pins
    assert "--trio-stress" in cmd and "--trio-record" not in cmd and cmd[cmd.index("--max-per-slot") + 1] == "1"
    assert cmd[cmd.index("--slots") + 1] == "youtube=2,twitch=1,tiktok=1" and "--trio-yt-fullscreen" in cmd
    assert "--dpms-off" in cmd and box[-1] == "/o/box_record.jsonl" and box[1].endswith("gpu_record.py")
    a.yt_fullscreen = False
    _, cmd = D.commands(a, {"youtube": 1}, ["yt1"], Path("/o"))
    assert "geometry" not in " ".join(cmd) and "--trio-yt-fullscreen" not in cmd


def test_readings():
    rows = _ticks(1000, 30, 1080, 60.0, dropped_per_s=0.6)
    rows += [["1030000", "stall", "1080", "3.5", "False", "0", "False", "", ""],       # device: 3.5 s ahead
             ["1034000", "start", "1080", "", "False", "0", "False", "", ""],
             ["1040000", "stall", "1080", "0.2", "False", "0", "False", "", ""],       # network
             ["1046000", "start", "1080", "", "False", "0", "False", "", ""],
             ["1050000", "stall", "1080", "None", "False", "0", "False", "", ""],      # no reading
             ["1051000", "video_start", "1080", "", "False", "0", "False", "", ""]]
    rows += _ticks(1060, 20, 720, 30.0, frames0=5000)
    rows += _ticks(1080, 10, 720, 30.0, frames0=6000, paused=True)
    rows.sort(key=lambda r: float(r[0]))
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d) / "_trio_stress" / "youtube" / "yt_900001"
        sd.mkdir(parents=True)
        _labels(sd / "labels.csv", rows)
        (sd / "meta.json").write_text(json.dumps({"started_at": 990.0, "ended_at": 1700.0, "platform": "youtube",
                                                  "hold_state": "10", "concurrency": {"tag": "yt1"}}))
        st = D.stalls(D.read_labels(sd / "labels.csv"))
        assert [(x["cause"], x["s"]) for x in st] == [("device", 4.0), ("network", 6.0), ("unattributed", 1.0)], st
        r = D.session_reading(sd)
        assert r["stall_s"] == {"device": 4.0, "network": 6.0, "unattributed": 1.0, "load": 0.0} and r["slot"] == "yt1", r
        assert r["playing_s"] == 51.0 and r["device_stall_share"] == round(4.0 / r["playing_s"], 4), r
        assert r["tiers"]["1080"]["rendered_fps"] == 60.0 and r["tiers"]["720"]["rendered_fps"] == 30.0, r["tiers"]
        assert r["tiers"]["1080"]["dropped_share"] == 0.01 and r["top_tier"] == 1080 and r["device_stall_readable"]
        tt = Path(d) / "_trio_stress" / "tiktok" / "ti_900002"                  # tt: every buffer reading missing
        tt.mkdir(parents=True)
        _labels(tt / "labels.csv", [[r[0], r[1], r[2], "None", *r[4:]] for r in _ticks(1000, 30, 720, 25.0)]
                + [["1031000", "stall", "720", "None", "False", "0", "False", "", ""]])
        (tt / "meta.json").write_text(json.dumps({"started_at": 990.0, "ended_at": 1700.0, "platform": "tiktok",
                                                  "concurrency": {"tag": "tt1"}}))
        rt = D.session_reading(tt)
        assert rt["device_stall_readable"] is False and rt["tt_60fps_tier"].startswith("tt 60 fps tier not reached"), rt


def _r(tag, share, readable=True, dur=700.0, tt60=None, top_s=None):
    # restart prereg (short dry runs): the gate's support, >= 300 s at every slot's top tier with every session at once
    return {"slot": tag, "duration_s": dur, "device_stall_readable": readable, "device_stall_share": share,
            "started_at": 0.0, "ended_at": dur, "seconds_at_top_tier": dur - 60.0 if top_s is None else top_s,
            "top_state_spans": [[20.0, dur]], "first_playing_at": 10.0,
            **({"tt_60fps_tier": tt60} if tt60 else {})}


def test_gate():
    box = {"swap_growth_kb": 0, "oom_kills": 0, "cpu_pct": {"p95": 70.0}, "gpu_above_report_line": []}
    g = D.gate([_r("yt1", 0.01), _r("tw1", 0.0), _r("tt1", None, readable=False, tt60="tt 60 fps tier not reached (x)")],
               box, ["yt1", "tw1", "tt1"])
    assert g["pass"] and g["short_of_headroom"] is False and g["slots"]["tt1"]["ok"] is None, g
    assert any("not reached" in n for n in g["notes"]) and any("not readable" in n for n in g["notes"])
    g = D.gate([_r("yt1", 0.03), _r("tw1", 0.0, dur=300.0)], dict(box, cpu_pct={"p95": 90.0}, gpu_above_report_line=["rcs0"]),
               ["yt1", "tw1", "tt1"])
    assert not g["pass"] and g["short_of_headroom"] is True and len(g["fails"]) == 3, g      # share, short, no tt1
    assert any("GPU" in n for n in g["notes"])
    g = D.gate([_r("yt1", 0.0)], dict(box, swap_growth_kb=512, oom_kills=1), ["yt1"])
    assert not g["pass"] and len(g["fails"]) == 2, g
    assert D.gate([_r("yt1", 0.0)], None, ["yt1"])["fails"] == ["no device record"]


def test_loading_stalls_and_readability():
    """levi on yt_900001: stalls inside an ad or on a 0 x 0 element (yt's ad / content swaps) are loading stalls,
    reported apart; with no playback stall a slot is readable (share 0); playback stalls none of which carries
    buffer-ahead are the stated gap."""
    rows = _ticks(1000, 30, 1080, 30.0) + [["1010000", "stall", "0", "None", "False", "0", "True", "", ""],
                                          ["1012000", "start", "1080", "", "False", "0", "False", "", ""],
                                          ["1020000", "stall", "0", "None", "False", "0", "False", "", ""],
                                          ["1021000", "video_start", "1080", "", "False", "0", "False", "", ""]]
    rows.sort(key=lambda r: float(r[0]))
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d) / "_trio_stress" / "youtube" / "yt_900001"
        sd.mkdir(parents=True)
        _labels(sd / "labels.csv", rows)
        (sd / "meta.json").write_text(json.dumps({"started_at": 990.0, "ended_at": 1700.0, "platform": "youtube",
                                                  "concurrency": {"tag": "yt1"}}))
        r = D.session_reading(sd)
        assert (r["load_stalls"], r["playback_stalls"], r["device_stall_readable"], r["device_stall_share"]) == (2, 0, True, 0.0), r
        assert r["stall_s"]["load"] == 3.0, r


def test_stop_recorder():
    """The recorder is stopped by its own pid (its header) through sudo -n kill; a recorder that ignores TERM is
    killed and stop_ok is false; stop_ok is true when it exits after TERM."""
    class Box:
        def __init__(self, exits):
            self.exits, self.done = exits, False

        def send_signal(self, s):
            pass

        def wait(self, timeout=None):
            if self.exits or self.done:
                self.done = True
                return 0
            raise subprocess.TimeoutExpired("x", timeout)

        def poll(self):
            return 0 if (self.exits or self.done) else None
    calls = []

    def run(cmd, capture_output=True):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)
    with tempfile.TemporaryDirectory() as d:
        rec = Path(d) / "box_record.jsonl"
        rec.write_text(json.dumps({"header": True, "pid": 4242}) + "\n")
        r = D.stop_recorder(Box(True), rec, run=run, wait_s=0.01)
        assert r == {"pid": 4242, "killed": False, "term_rc": 0, "stop_ok": True} and calls == [["sudo", "-n", "kill", "-TERM", "4242"]], r
        calls.clear()
        b = Box(False)
        orig = b.wait
        b.wait = lambda timeout=None: (setattr(b, "done", True) or 0) if ["sudo", "-n", "kill", "-KILL", "4242"] in calls else orig(timeout)
        r = D.stop_recorder(b, rec, run=run, wait_s=0.01)
        assert r["killed"] and r["stop_ok"] is False and calls[-1] == ["sudo", "-n", "kill", "-KILL", "4242"], (r, calls)



def test_a_cut_run_tears_its_tree_down():
    """levi (shagun's 7de5518 cut): orchestrate runs in its own session; decode_stress forwards TERM / INT / HUP to it
    (its drain), kills its group after the grace, always stops the recorder, and refuses a launch while a tree lives."""
    import os
    import signal
    src = (D.Path(D.__file__)).read_text()
    assert "start_new_session=True" in src and "(signal.SIGTERM, signal.SIGINT, signal.SIGHUP)" in src
    assert src.index("finally:\n        t1 = time.time()\n        rep[\"box_record_stop\"] = stop_recorder(") > src.index("wait_or_stop(orch, stop)")
    # the group kill, on a real child in its own session
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    t = [0.0]
    rc = D.wait_or_stop(child, {"t": -10.0}, clock=lambda: t[0], sleep=lambda _: None, grace_s=5.0)
    assert rc == -signal.SIGKILL, rc
    # no stop signal: it just waits
    done = subprocess.Popen(["true"])
    assert D.wait_or_stop(done, {"t": None}, sleep=lambda _: None) == 0
    # a live tree blocks a launch; a dead or absent one does not
    with tempfile.TemporaryDirectory() as d:
        tf = Path(d) / "tree.json"
        none = lambda *a, **k: SimpleNamespace(stdout="")
        assert D.live_tree(tf, run=none) == []
        tf.write_text(json.dumps({"pid": os.getppid(), "orchestrate_pid": 999999999}))
        assert D.live_tree(tf, run=none) == [f"pid {os.getppid()}"]
        tf.write_text(json.dumps({"orchestrate_pid": 999999999}))
        assert D.live_tree(tf, run=none) == []
        ps = lambda *a, **k: SimpleNamespace(stdout="4321 python3 orchestrate.py --batch x --concurrent --trio-stress\n"
                                                    "4322 python3 orchestrate.py --batch y --concurrent\n")
        assert D.live_tree(tf, run=ps) == ["orchestrate 4321"]

def test_all_top_window_and_reread():
    """levi (prereg 40843f81): the decode minimum is the seconds with every slot at its top shaping state (net_labels
    rows of the held state) and every session running, at whatever tier the ABR holds; each slot's seconds per tier
    over them; the device record read over the same seconds; --reread re-gates a finished stress from its files."""
    def net(path, rows):
        path.write_text("".join(json.dumps({"timestamp": t, "state": st, "speed": 7000}) + "\n" for t, st in rows))
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out"
        out.mkdir()
        sds = []
        # yt1 runs 1000-1420 at 720, shaped at 10 from 1015, loading (paused ticks) until 1060 (levi on 0097779: the
        # window opens at its first playing tick); tw1 runs 1030-1450 and swings 1080 / 720 (the ABR's oscillation at
        # the top state), shaped at 10 from 1040, one sub-step at state 7 (1100-1120): two spans
        for tag, plat, t0, t1, shaped, ticks in (
                ("yt1", "youtube", 1000.0, 1420.0, [(1015.0, "10")],
                 _ticks(1001, 59, 720, 0.0, paused=True) + _ticks(1060, 359, 720, 30.0)),
                ("tw1", "twitch", 1030.0, 1450.0, [(1040.0, "10"), (1100.0, "7"), (1120.0, "10"), (1300.0, "10")],
                 _ticks(1031, 100, 1080, 60.0) + _ticks(1131, 318, 720, 30.0, frames0=6000))):
            sd = Path(d) / "_trio_stress" / plat / f"{tag}_900001"
            sd.mkdir(parents=True)
            _labels(sd / "labels.csv", ticks)
            net(sd / "net_labels.txt", shaped)
            (sd / "meta.json").write_text(json.dumps({"started_at": t0, "ended_at": t1, "platform": plat,
                                                      "hold_state": "10", "concurrency": {"tag": tag}}))
            sds.append(sd)
        assert D.top_state_spans(sds[1], "10", 1450.0) == [[1040.0, 1100.0], [1120.0, 1450.0]]
        assert D.intersect([[[0, 10], [20, 30]], [[5, 25]]]) == [[5, 10], [20, 25]] and D.intersect([]) == []
        # CPU busy at startup and in the gap between the spans (levi: the union of the spans, not their hull)
        rows = [{"t": t, "cpu_pct": 95.0 if t < 1060 or 1100 <= t < 1120 else 50.0, "swap_used_kb": 0, "oom_kill": 0,
                 "gpu_busy_pct": {"rcs0": 40.0}} for t in range(1000, 1460, 2)]
        (out / "box_record.jsonl").write_text(json.dumps({"header": True, "gpu_source": "x"}) + "\n"
                                              + "".join(json.dumps(r) + "\n" for r in rows))
        rep = {"what": "t", "composition": {"youtube": 1, "twitch": 1}, "slots": ["yt1", "tw1"], "state": "10",
               "cmd": ["orchestrate.py", "--episodes", "7"], "t0": 995.0, "t1": 1460.0, "sessions": [str(s) for s in sds],
               "gate": {"status": "insufficient"}}
        (out / "report.json").write_text(json.dumps(rep))
        rr = D.reread(out / "report.json")
        g = rr["gate"]
        assert g["window"] == [[1060.0, 1100.0], [1120.0, 1420.0]], g["window"]
        assert g["support"]["support"] == {"all_top_state_s": 340.0} and g["status"] == "pass", g
        assert {k: rr["box_window"][k] for k in ("t0", "t1", "spans", "over")} == {
            "t0": 1060.0, "t1": 1420.0, "spans": [[1060.0, 1100.0], [1120.0, 1420.0]], "over": "all-top window (union of spans)"}
        # startup's and the gap's 95 % outside (over the hull: 10 of 180 rows at 95, p95 95)
        assert rr["box_record"]["cpu_pct"]["p95"] == 50.0 and rr["box_record"]["n"] == 170, rr["box_record"]
        assert g["short_of_headroom"] is False
        assert rr["box_window"]["oom_kills"] == {"whole": 0, "spans": 0} and rr["box_window"]["counters_over"]["t0"] == 995.0
        tw = g["slots"]["tw1"]["tiers_all_top_s"]
        assert tw == {"720": 289.0, "1080": 50.0} and g["slots"]["tw1"]["top_tier_all_top"] == 1080, tw
        assert g["slots"]["yt1"]["tiers_all_top_s"] == {"720": 338.0}, g["slots"]["yt1"]   # its last tick 1418
        assert any(n.startswith("tw1: top tier 1080 held 50.0 s") for n in g["notes"]), g["notes"]
        assert rr["readings"][0]["first_playing_at"] == 1060.0
        assert rr["reread_of"]["gate"] == {"status": "insufficient"}
        assert D.main(["--reread", str(out / "report.json")]) == 0 and (out / "report_reread.json").exists()
        # levi on fb78845: swap and OOM are counters, read over the whole stress: an OOM kill at startup (before the
        # window) and swap growing before it still fail, though the window's own rows show neither
        rows = [dict(r, oom_kill=1 if r["t"] >= 1030 else 0, swap_used_kb=64 if r["t"] >= 1030 else 0) for r in rows]
        (out / "box_record.jsonl").write_text(json.dumps({"header": True, "gpu_source": "x"}) + "\n"
                                              + "".join(json.dumps(r) + "\n" for r in rows))
        rr = D.reread(out / "report.json")
        assert rr["gate"]["status"] == "fail" and rr["gate"]["fails"] == ["swap grew 64 kB", "1 OOM kill(s)"], rr["gate"]
        assert rr["box_window"]["oom_kills"] == {"whole": 1, "spans": 0}, rr["box_window"]
        assert rr["box_window"]["swap_growth_kb"] == {"whole": 64, "spans": 0}, rr["box_window"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("test_decode_stress: ok")
