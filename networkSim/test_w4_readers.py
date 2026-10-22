#!/usr/bin/env python3
"""W4's readers on synthetic session dirs with known answers (supervisor 03:44; laptop fixtures, no chaos):
 - ops/diag/shaper_realised.py: a backlogged class delivering the configured rate and a capture at 1.0x / 0.8x of it
   read class ratio 1, capture ratio 1 / 0.8 (in band / out of band), capture/class 1 / 0.8; upstream frames are
   ignored; a dir without mergedPackets.csv is skipped, named;
 - ops/diag/tc_conservation.py: two slots plus unshaped traffic read the unshaped share as the residual in every
   window; a slot whose samples miss part of its session leaves those windows uncovered (skipped, counted); an interface
   that received less than the classes delivered is flagged negative beyond the delay line.

    python3 test_w4_readers.py        (exit 0 = pass)
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIAG = HERE.parent / "ops" / "diag"


def _mod(name: str):
    spec = importlib.util.spec_from_file_location(name, DIAG / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SR, TC = _mod("shaper_realised"), _mod("tc_conservation")
T0 = 1.79e9


def _session(d: Path, name: str, kbit: float = 1000.0, capture_factor: float = 1.0, seconds: int = 30,
             capture: bool = True, delay_ms: int = 40) -> Path:
    sd = d / name
    sd.mkdir(parents=True)
    (sd / "net_labels.txt").write_text(json.dumps({"timestamp": T0, "speed": kbit, "state": "5", "delay_ms": delay_ms}) + "\n")
    bps = kbit * 1000 / 8
    (sd / "tc_stats.jsonl").write_text("".join(json.dumps({"ts": T0 + i, "in": {"bytes": int(bps * i), "backlog": 50000, "drops": 0}})
                                               + "\n" for i in range(seconds + 1)))
    if capture:
        rows = ["Timestamp,Direction,PacketLength"]
        frame = 1250
        n = int(bps * capture_factor / frame)                 # frames per second downstream
        for i in range(seconds):
            for k in range(n):
                rows.append(f"{T0 + i + (k + 0.5) / n},1,{frame}")
            rows.append(f"{T0 + i + 0.5},0,66")              # an upstream ACK, never counted
        (sd / "mergedPackets.csv").write_text("\n".join(rows) + "\n")
    return sd


def test_shaper_realised():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        good, low, bare = _session(d, "s_good"), _session(d, "s_low", capture_factor=0.8), _session(d, "s_bare", capture=False)
        rg, rl, rb = SR.session(good), SR.session(low), SR.session(bare)
        assert rg["n_spans"] == 1 and rl["n_spans"] == 1, (rg, rl)
        sg, sl = rg["spans"][0], rl["spans"][0]
        assert abs(sg["ratio"] - 1) < 1e-6 and abs(sl["ratio"] - 1) < 1e-6, (sg, sl)
        assert abs(sg["capture_ratio"] - 1) < 0.01 and abs(sl["capture_ratio"] - 0.8) < 0.01, (sg, sl)
        assert abs(sl["capture_over_class"] - 0.8) < 0.01 and sg["state_at_t0"] == "5"
        assert rg["capture_in_band"] == 1 and rl["capture_in_band"] == 0
        assert abs(rg["per_state_capture_ratio"]["5"]["median"] - 1) < 0.01
        assert rb.get("skipped") == "missing mergedPackets.csv", rb
        assert SR.main([str(good), str(low), "--out", str(d / "r.json")]) == 0


def _slot(d: Path, name: str, tag: str, rate_bps: float, t_end: int = 60, gap: tuple | None = None, rx_bps: float = 0.0):
    sd = d / name
    sd.mkdir(parents=True)
    (sd / "meta.json").write_text(json.dumps({"started_at": T0, "ended_at": T0 + t_end, "concurrency": {"tag": tag}}))
    (sd / "net_labels.txt").write_text(json.dumps({"timestamp": T0, "speed": rate_bps * 8 / 1000, "state": "7", "delay_ms": 40}) + "\n")
    rows = []
    for i in range(t_end + 1):
        if gap and gap[0] <= i <= gap[1]:
            continue
        rows.append({"ts": T0 + i, "in": {"bytes": int(rate_bps * i), "backlog": 0, "drops": 0},
                     "iface": {"rx_bytes": int(rx_bps * i)}, "set_rates_kbit": {tag: rate_bps * 8 / 1000}})
    (sd / "tc_stats.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return sd


def test_tc_conservation():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        a = _slot(d, "a", "yt1", 100_000, rx_bps=160_000)
        b = _slot(d, "b", "tw1", 50_000, rx_bps=160_000)
        r = TC.conservation([TC.load(a), TC.load(b)], 10.0)
        assert r["windows"] == 6 and r["skipped"]["uncovered_slot"] == 0, r
        assert abs(r["residual_share"]["median"] - 10_000 / 160_000) < 1e-3 and r["n_negative_beyond_delay_line"] == 0, r
        g = _slot(d, "g", "tw1", 50_000, gap=(15, 35), rx_bps=160_000)
        r = TC.conservation([TC.load(a), TC.load(g)], 10.0)
        assert r["skipped"]["uncovered_slot"] >= 2 and r["windows"] <= 4, r
        n1, n2 = _slot(d, "n1", "yt1", 100_000, rx_bps=100_000), _slot(d, "n2", "tw1", 50_000, rx_bps=100_000)
        r = TC.conservation([TC.load(n1), TC.load(n2)], 10.0)
        assert r["n_negative_beyond_delay_line"] == r["windows"] > 0, r
        assert TC.main([str(a), str(b), "--out", str(d / "c.json")]) == 0


def main() -> int:
    test_shaper_realised()
    test_tc_conservation()
    print("test_w4_readers: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
