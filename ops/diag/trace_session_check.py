#!/usr/bin/env python3
"""G2, the dry trace window's read (LVQoE restart prereg §9; levi's rulings efb5e4cc): over the window's trace sessions
(meta.json shaper.regime = trace), read only from the session dirs, never the record root.

Per session:
  - the record: shaper.regime "trace" with its plan / upper / lower / clip shares, steering_fallback "trace regime";
    every net_labels row state null, regime "trace", rate_band_state and scheduled_t set;
  - fidelity (networkSim/traces/fidelity.py): the byte-total ratio over backlogged spans of the slot's ingress class
    (tc_stats.jsonl), each in [0.9, 1.15]; the per-second ratio reported only;
  - kernel capture drops 0 (meta concurrency.capture_drops);
  - the low band crossed: rate_band_state into stuck.LOW_HOLD_STATES and out again, with the hold logged (tt:
    meta.player_dead_watch.low_spans; every platform: the spans the hold reads, stuck.low_spans on the rows).
Pooled: the apply lag (applied - scheduled per change) p99 <= 0.5 s at the window's N, beside the box CPU when given
(--box-record: gpu_record's box_record.jsonl). G2 needs >= 1 session crossing a low band with the hold logged.

    python3 ops/diag/trace_session_check.py <session dir> ... [--out g2.json]
Exit 0 = pass, 1 = a gate failed, 2 = no trace session read.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "networkSim"))
import stuck  # noqa: E402
from traces import fidelity as F  # noqa: E402


def _json(p: Path):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def _lines(p: Path) -> list[dict]:
    if not Path(p).exists():
        return []
    out = []
    for line in Path(p).read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def session(sd: Path) -> dict | None:
    meta = _json(sd / "meta.json") or {}
    sh = meta.get("shaper") or {}
    if sh.get("regime") != "trace":
        return None
    labels = _lines(sd / "net_labels.txt")
    tc = _lines(sd / "tc_stats.jsonl")
    bad_rows = [i for i, x in enumerate(labels) if x.get("state") is not None or x.get("regime") != "trace"
                or not x.get("rate_band_state") or not isinstance(x.get("scheduled_t"), (int, float))]
    ch = F.session_changes(labels)
    spans = F.backlogged_spans(F.session_samples(tc), ch)
    bands = [str(x.get("rate_band_state")) for x in labels]
    low = [b.isdigit() and int(b) in stuck.LOW_HOLD_STATES for b in bands]
    crossed = any(not a and b for a, b in zip(low, low[1:])) and any(a and not b for a, b in zip(low, low[1:]))
    tt_spans = ((meta.get("player_dead_watch") or {}).get("low_spans")) if meta.get("platform") == "tiktok" else None
    cd = ((meta.get("concurrency") or {}).get("capture_drops") or {}).get("dropped_kernel_total")
    rec = {"session": str(sd), "platform": meta.get("platform"),
           "record": {"trace": bool(sh.get("trace")), "steering_fallback": meta.get("steering_fallback"),
                      "rows": len(labels), "rows_not_trace_shaped": bad_rows[:20], "n_rows_not_trace_shaped": len(bad_rows),
                      "ok": bool(sh.get("trace")) and not bad_rows and meta.get("steering_fallback") == "trace regime"},
           "source": (sh.get("trace") or {}).get("plan", {}).get("source"),
           "upper_kbit": (sh.get("trace") or {}).get("upper_kbit"), "clip": (sh.get("trace") or {}).get("clip"),
           "fidelity": {"spans": spans, "gate": F.ratio_gate(spans), "per_second": F.per_second_ratio(F.session_samples(tc), ch)},
           "capture_drops": {"dropped_kernel_total": cd, "ok": cd == 0},
           "low_band": {"crossed": bool(crossed), "rows_low": sum(low), "hold_spans_from_rows": stuck.low_spans(labels),
                        "tt_watch_low_spans": tt_spans,
                        "hold_logged": bool(stuck.low_spans(labels)) and (tt_spans is None or bool(tt_spans))},
           "lags": F.apply_lags(labels)}
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--box-record", type=Path, default=None, help="gpu_record's box_record.jsonl for the window (CPU)")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    rows = [r for r in (session(sd) for sd in a.sessions) if r is not None]
    if not rows:
        print("no trace session read", file=sys.stderr)
        return 2
    lags = [x for r in rows for x in r.pop("lags")]
    cpu = None
    if a.box_record and a.box_record.exists():
        vals = [x.get("cpu_pct") for x in _lines(a.box_record) if isinstance(x.get("cpu_pct"), (int, float))]
        cpu = {"samples": len(vals), "p50": sorted(vals)[len(vals) // 2] if vals else None, "max": max(vals) if vals else None}
    lag = {**F.lag_gate(lags), "box_cpu": cpu}
    crossed = [r["session"] for r in rows if r["low_band"]["crossed"] and r["low_band"]["hold_logged"]]
    gates = {"record": all(r["record"]["ok"] for r in rows),
             "fidelity": all(r["fidelity"]["gate"] == "pass" for r in rows),
             "capture_drops": all(r["capture_drops"]["ok"] for r in rows),
             "apply_lag": lag["gate"] == "pass", "low_band_crossed_with_hold": bool(crossed)}
    rep = {"rule": F.RULE, "sessions": rows, "apply_lag": lag, "low_band_crossing_sessions": crossed, "gates": gates,
           "pass": all(gates.values())}
    text = json.dumps(rep, indent=1, default=str)
    if a.out:
        a.out.write_text(text)
    print(json.dumps({"pass": rep["pass"], "gates": gates, "sessions": len(rows), "apply_lag_p99": lag["p99"]}, indent=1))
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
