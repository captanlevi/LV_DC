#!/usr/bin/env python3
"""The counted rounds' mechanical stop rule (supervisor 2026-10-19, after counted round 1's chain stopped on two
non-gating items): stop only where the signed round reader says a gating item failed, and report everything else.

Read only. Stop (exit 1, "BAD ...") on:
  - any gate2_items item whose ok is False: items 1 / 2 (a verify / check reason outside single mode's frozen inventory,
    or a missing verify / check), 4a's visibility, 7, 9 (capture drops), 11 (a payload split or shared flow outside the
    link-local multicast exception, or one undecided), 12-14, the per-session teardown. The reader decides what gates:
    reported items (4a's rendered fps / device_limited, 5, 8, fullscreen) carry ok None and never stop;
  - a session missing from the reader's output, in _failed, or with a reader error;
  - the run's teardown.json not clean;
  - a session's device record (gpu_record --stamp: <session>/box_record.jsonl) missing or covering < 0.95 of its
    seconds (Addendum T, D1); a covered second holds a data line (has gpu_busy_pct).
Everything else goes to the report (non-gating items with a reading, the device-limited tiers).

    python3 ops/round_stop_checks.py --gate2 <gate2_items.json> --teardown <teardown.json> <session dir> ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COVERAGE_MIN = 0.95


def coverage(sd: Path) -> dict:
    """The session's device-record coverage from its stamped box_record.jsonl (header stamp window [t0 - 1, t1 + 1])."""
    p = Path(sd) / "box_record.jsonl"
    if not p.exists():
        return {"ok": False, "why": "no box_record.jsonl (gpu_record --stamp not run?)"}
    lines = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    hdr = lines[0] if lines and lines[0].get("header") else {}
    w = (hdr.get("stamp") or {}).get("window")
    if not w:
        return {"ok": False, "why": "no stamp window in the header"}
    t0, t1 = w[0] + 1, w[1] - 1
    secs = max(int(t1 - t0), 0)
    have = {int(r["t"] - t0) for r in lines[1:] if "gpu_busy_pct" in r and t0 <= r["t"] < t1}
    share = round(len([s for s in have if 0 <= s < secs]) / secs, 4) if secs else None
    return {"seconds": secs, "covered_share": share, "ok": bool(share is not None and share >= COVERAGE_MIN)}


def gating(row: dict) -> tuple[list[str], list[str]]:
    """(gating failures, reported notes) of one gate2_items session row."""
    bad, notes = [], []
    if "error" in row:
        return [f"reader error: {row['error'][:120]}"], notes
    for k, v in (row.get("items1_2") or {}).items():
        if v.get("ok") is False:
            bad.append(f"item {k}: {v.get('reading')}")
        elif v.get("ok") is None:
            notes.append(f"item {k}: {v.get('reading')}")
    for k in ("item4a", "item7", "item9", "item11", "item12", "item13", "item14", "teardown"):
        v = row.get(k)
        if isinstance(v, dict) and v.get("ok") is False:
            bad.append(f"{k}: " + json.dumps({x: y for x, y in v.items() if x not in ("rendered_fps",)}, default=str)[:200])
    lim = (row.get("item4a") or {}).get("device_limited_tiers")
    if lim:
        notes.append(f"item4a device_limited {lim} (reported)")
    return bad, notes


def check(gate2: dict, teardown: dict | None, sessions: list[Path]) -> dict:
    rows = {Path(r.get("session", "")).name: r for r in gate2.get("sessions") or []}
    bad, report = [], {}
    if teardown is not None and not teardown.get("clean"):
        bad.append("run teardown not clean: " + json.dumps({k: v.get("ok") for k, v in (teardown.get("checks") or {}).items()}))
    for sd in sessions:
        sd = Path(sd)
        name = sd.name
        if "_failed" in sd.parts or not sd.exists():
            bad.append(f"{name}: in _failed or missing")
            continue
        r = rows.get(name)
        if r is None:
            bad.append(f"{name}: not in the reader's output")
            continue
        b, n = gating(r)
        cov = coverage(sd)
        if not cov["ok"]:
            b.append(f"device record: {cov.get('why') or cov.get('covered_share')}")
        bad += [f"{name} {x}" for x in b]
        report[name] = {"reported": n, "device_record": cov}
    return {"ok": not bad, "gating": bad, "report": report}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gate2", type=Path, required=True, help="gate2_items --out json of the round")
    ap.add_argument("--teardown", type=Path, default=None, help="the run's teardown.json")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("sessions", nargs="+", type=Path)
    a = ap.parse_args(argv)
    res = check(json.loads(a.gate2.read_text()), json.loads(a.teardown.read_text()) if a.teardown else None, a.sessions)
    if a.out:
        a.out.write_text(json.dumps(res, indent=1))
    print(("OK " + " ".join(Path(s).name for s in a.sessions)) if res["ok"] else "BAD " + " | ".join(res["gating"]))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
