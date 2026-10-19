#!/usr/bin/env python3
"""The counted rounds' mechanical stop rule (supervisor 2026-10-19, after counted round 1's chain stopped on two
non-gating items): stop only where the signed round reader says a gating item failed, and report everything else.

Read only. Stop (exit 1, "BAD ...") on:
  - any gate2_items item whose ok is False: items 1 / 2 (a verify / check reason outside single mode's frozen inventory,
    or a missing verify / check), 4a's visibility, 7, 9 (capture drops), 11 (a payload split or shared flow outside the
    link-local multicast exception, or one undecided), 12-14, the per-session teardown. The reader decides what gates:
    reported items (4a's rendered fps / device_limited, 5, 8, fullscreen) carry ok None and never stop;
  - a session dir missing, a session missing from the reader's output, or a reader error. A session in _failed stops
    only through the reader (levi): its reasons read through the same inventory rule, so a session failed for reasons
    single mode also produced (e.g. check class player_frozen) is reported, a new kind stops;
  - the run's teardown.json not clean;
  - a session's device record (gpu_record --stamp: <session>/box_record.jsonl) missing or covering < 0.95 of its
    seconds (Addendum T, D1); a covered second holds a data line (has gpu_busy_pct).
Everything else goes to the report (non-gating items with a reading, the device-limited tiers).

Systematic exclusion (levi's rule, supervisor 2026-10-19), with --inventory (brad's json with base_rates; both shas
checked) and --history (the earlier counted rounds' --out jsons, oldest first). Over the sessions whose verify / check
did not PASS, the coarse kinds per platform (brad's kind rule; check's class as "class:<x>"; a kind counts once per
session and file). Stop and diagnose when:
  - the same kind on the same platform appears in this round and the previous counted round; or
  - over all counted rounds, a platform's sessions carrying a kind number >= 3 and their share is >= 2 x the kind's
    single-mode base rate on that platform (base_rates[file][platform].coarse[kind].rate).
The per-session exclusion stays (items 1 / 2 read through the inventory as before).

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


def load_base_rates(inv: dict, expect: str | None) -> dict:
    """brad's base_rates (collection_start_items.py, brad/inventory-base-rates befc542), refused unless they hash to the
    file's base_rates_sha (sha256 of json.dumps(base_rates, sort_keys=True)) with the expected prefix."""
    import hashlib
    sha = hashlib.sha256(json.dumps(inv["base_rates"], sort_keys=True).encode()).hexdigest()
    if sha != inv.get("base_rates_sha") or not expect or not sha.startswith(expect):
        raise SystemExit(f"refused: base_rates sha {sha[:12]} (file {str(inv.get('base_rates_sha'))[:12]}, expected {expect})")
    return inv["base_rates"]


def round_kinds(gate2: dict) -> dict:
    """{"n_sessions": {platform: n}, "kinds": {platform: {file: {kind: [sessions]}}}} over this round's sessions: the
    coarse kinds of every verify / check that did not PASS."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "networkSim"))
    from gate2_items import reason_kind
    n, kinds = {}, {}
    for r in gate2.get("sessions") or []:
        sd = Path(r.get("session", ""))
        plat = sd.parent.name
        n[plat] = n.get(plat, 0) + 1
        for item, f in (("1", "verify"), ("2", "check")):
            v = (r.get("items1_2") or {}).get(item) or {}
            if v.get("ok") is True:
                continue
            rs = list(v.get("reasons") or [])
            c = v.get("session_class")
            if f == "check" and c is not None and c not in ("normal", "censored"):
                rs.append(f"class:{c}")
            for k in {reason_kind(x, coarse=True) for x in rs}:
                kinds.setdefault(plat, {}).setdefault(f, {}).setdefault(k, []).append(sd.name)
    return {"n_sessions": n, "kinds": kinds}


def systematic(cur: dict, history: list[dict], base: dict) -> list[str]:
    """Levi's systematic-exclusion stops (module docstring) for this round's kinds against the earlier rounds'."""
    bad = []
    prev = history[-1]["round_kinds"] if history else None
    for plat, fk in cur["kinds"].items():
        for f, ks in fk.items():
            for k in ks:
                if prev and k in ((prev.get("kinds") or {}).get(plat) or {}).get(f, {}):
                    bad.append(f"systematic: {f} '{k}' on {plat} in 2 consecutive counted rounds")
    rounds = [h["round_kinds"] for h in history] + [cur]
    for plat in {p for r in rounds for p in r["n_sessions"]}:
        n = sum(r["n_sessions"].get(plat, 0) for r in rounds)
        for f in ("verify", "check"):
            ev: dict = {}
            for r in rounds:
                for k, ss in ((r["kinds"].get(plat) or {}).get(f) or {}).items():
                    ev[k] = ev.get(k, 0) + len(set(ss))
            for k, e in ev.items():
                b = (((base.get(f) or {}).get(plat) or {}).get("coarse") or {}).get(k)
                if b is None or not n:
                    continue                                   # a kind single mode never produced gates in items 1 / 2
                if e >= 3 and e / n >= 2 * b["rate"]:
                    bad.append(f"systematic: {f} '{k}' on {plat}: {e} of {n} counted sessions, >= 2 x single mode's "
                               f"{b['rate']:.3f}")
    return bad


def check(gate2: dict, teardown: dict | None, sessions: list[Path], history: list[dict] | None = None,
          base: dict | None = None) -> dict:
    rows = {Path(r.get("session", "")).name: r for r in gate2.get("sessions") or []}
    bad, report = [], {}
    if teardown is not None and not teardown.get("clean"):
        bad.append("run teardown not clean: " + json.dumps({k: v.get("ok") for k, v in (teardown.get("checks") or {}).items()}))
    for sd in sessions:
        sd = Path(sd)
        name = sd.name
        if not sd.exists():
            bad.append(f"{name}: session dir missing")
            continue
        r = rows.get(name)
        if r is None:
            bad.append(f"{name}: not in the reader's output")
            continue
        b, n = gating(r)
        cov = coverage(sd)
        if not cov["ok"]:
            b.append(f"device record: {cov.get('why') or cov.get('covered_share')}")
        if "_failed" in sd.parts:
            n.insert(0, "in _failed (stops only on the reader's gating items)")
        bad += [f"{name} {x}" for x in b]
        report[name] = {"reported": n, "device_record": cov}
    rk = round_kinds(gate2)
    if base is not None:
        bad += systematic(rk, history or [], base)
    return {"ok": not bad, "gating": bad, "report": report, "round_kinds": rk,
            "systematic_rule": "on" if base is not None else "off (no --inventory)"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gate2", type=Path, required=True, help="gate2_items --out json of the round")
    ap.add_argument("--teardown", type=Path, default=None, help="the run's teardown.json")
    ap.add_argument("--inventory", type=Path, default=None, help="brad's inventory json with base_rates")
    ap.add_argument("--inventory-sha", default=None, help="its inventory_sha prefix (checked)")
    ap.add_argument("--base-rates-sha", default=None, help="its base_rates_sha prefix (checked)")
    ap.add_argument("--history", type=Path, nargs="*", default=[], help="earlier counted rounds' --out jsons, oldest first")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("sessions", nargs="+", type=Path)
    a = ap.parse_args(argv)
    base = None
    if a.inventory:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "networkSim"))
        from gate2_items import load_inventory
        inv = load_inventory(a.inventory, a.inventory_sha)
        base = load_base_rates(inv, a.base_rates_sha)
    hist = [json.loads(h.read_text()) for h in a.history]
    res = check(json.loads(a.gate2.read_text()), json.loads(a.teardown.read_text()) if a.teardown else None, a.sessions,
                hist, base)
    if a.out:
        a.out.write_text(json.dumps(res, indent=1))
    print(("OK " + " ".join(Path(s).name for s in a.sessions)) if res["ok"] else "BAD " + " | ".join(res["gating"]))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
