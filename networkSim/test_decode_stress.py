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
        assert [(x["cause"], x["s"]) for x in st] == [("device", 4.0), ("network", 6.0), ("unknown", 1.0)], st
        r = D.session_reading(sd)
        assert r["stall_s"] == {"device": 4.0, "network": 6.0, "unknown": 1.0} and r["slot"] == "yt1", r
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


def _r(tag, share, readable=True, dur=700.0, tt60=None):
    return {"slot": tag, "duration_s": dur, "device_stall_readable": readable, "device_stall_share": share,
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("test_decode_stress: ok")
