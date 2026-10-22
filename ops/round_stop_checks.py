#!/usr/bin/env python3
"""The counted rounds' mechanical stop rule (supervisor 2026-10-19, after counted round 1's chain stopped on two
non-gating items): stop only where the signed round reader says a gating item failed, and report everything else.

Read only. Stop (exit 1, "BAD ...") on:
  - any gate2_items item whose ok is False: items 1 / 2 (a verify / check reason outside single mode's frozen inventory,
    or a missing verify / check), 3 (frames coverage below single mode's min, or not read; levi 2026-10-20: a FAIL there
    is a stop-and-diagnose of the extractor), 4a's visibility, 7, 9 (capture drops), 11 (a payload split or shared flow outside the
    link-local multicast exception, or one undecided), 12-14, the per-session teardown. The reader decides what gates:
    reported items (4a's rendered fps / device_limited, 5, 8, fullscreen) carry ok None and never stop;
  - a session dir missing, a session missing from the reader's output, or a reader error. A session in _failed stops
    only through the reader (levi): its reasons read through the same inventory rule, so a session failed for reasons
    single mode also produced (e.g. check class player_frozen) is reported, a new kind stops;
  - the run's teardown.json not clean;
  - a session's device record (gpu_record --stamp: <session>/box_record.jsonl) missing or covering < 0.95 of its
    seconds (Addendum T, D1); a covered second holds a data line (has gpu_busy_pct).
Everything else goes to the report (non-gating items with a reading, the device-limited tiers).

startup_failure (levi's ruling 2026-10-19, after yt_100007 on @FuboSports): a session whose check class is
startup_failure stops the chain only if its channel / room played normally before in either mode (an accepted PASS
session on the same key, single mode's root or the trio's), or a sibling in the same round also failed to start;
otherwise it reads "content, excluded": its items 1 / 2 do not gate (resource items still do), it is reported, and it
is no event for the systematic rule below (supervisor 2026-10-19: left out of this round's kinds and counts, listed in
round_kinds.content; an earlier round's content sessions likewise, from its list or re-read by the same rule).

Systematic exclusion (levi's rule, supervisor 2026-10-19), with --inventory (brad's json with base_rates; both shas
checked) and --history (the earlier counted rounds' --out jsons, oldest first). Over the sessions whose check did not
PASS (exclusions: a FAIL kind or a non-normal class, freezes included) and verify's mechanics kinds (VERIFY_MECHANICS:
chunk recall, flow attribution; they could be concurrency), the coarse kinds per platform (brad's kind rule; check's
class as "class:<x>"; a kind counts once per session). verify's label-based kinds (paused while labelled playing, label
agreement) and content bands (B/s per tier, the speed rule) are reported per round with their share
(round_kinds.reported), never a stop (supervisor 2026-10-19 after round 2; levi's split on 6584cef). Stop and diagnose
when:
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
# levi on 6584cef: verify's mechanics kinds can be concurrency, so they stay in the systematic rule; its label-based kinds
# (paused while labelled playing, label agreement) and content bands (B/s per tier, the speed rule) are reported only
VERIFY_MECHANICS = ("chunk recall", "selected flows carry")


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


# levi 11:37 (the fail-open sweep): every gating item a row of its mode must carry; one missing stops the chain as
# "schema: <item> missing" (before, a row without the item read as not gated)
REQUIRED_ITEMS = {"single": ("item3", "item4a"),
                  "trio": ("item3", "item4a", "item7", "item9", "item11", "item12", "item13", "item14", "teardown")}


def schema_missing(row: dict) -> list[str]:
    mode = row.get("mode")
    if mode not in REQUIRED_ITEMS:
        return [f"schema: mode {mode!r} unknown" if "mode" in row else "schema: mode missing"]
    out = [f"schema: {k} missing" for k in REQUIRED_ITEMS[mode] if not isinstance(row.get(k), dict)]
    i12 = row.get("items1_2")
    out += [f"schema: items1_2.{k} missing" for k in ("1", "2") if not isinstance((i12 or {}).get(k), dict)]
    return out


def gating(row: dict) -> tuple[list[str], list[str]]:
    """(gating failures, reported notes) of one gate2_items session row."""
    bad, notes = [], []
    if "error" in row:
        return [f"reader error: {row['error'][:120]}"], notes
    bad += schema_missing(row)
    bad += list((row.get("record") or {}).get("schema") or [])     # levi 16:42: is_record undetermined refuses
    for k, v in (row.get("items1_2") or {}).items():
        if v.get("ok") is False:
            bad.append(f"item {k}: {v.get('reading')}")
        elif v.get("ok") is None:
            notes.append(f"item {k}: {v.get('reading')}")
    for k in ("item3", "item4a", "item7", "item9", "item11", "item12", "item13", "item14", "teardown"):
        v = row.get(k)
        if isinstance(v, dict) and v.get("ok") is False:
            bad.append(f"{k}: " + json.dumps({x: y for x, y in v.items() if x not in ("rendered_fps",)}, default=str)[:200])
    if (row.get("item3") or {}).get("applicable") is False:    # levi 2026-10-20: no sized media response, bodies recovered
        notes.append(f"item3 {row['item3'].get('reading')} (reported)")
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


def round_kinds(gate2: dict, content: set | frozenset = frozenset()) -> dict:
    """{"n_sessions": {platform: n}, "kinds": {platform: {file: {kind: [sessions]}}}} over this round's sessions: the
    coarse kinds of every verify / check that did not PASS. A session in `content` (a startup_failure read as content,
    startup_failure_rule) is no collection event: left out of the kinds and the counts, listed under "content"."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "networkSim"))
    from gate2_items import reason_kind
    n, kinds, reported = {}, {}, {}
    for r in gate2.get("sessions") or []:
        sd = Path(r.get("session", ""))
        if sd.name in content:
            continue
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
            # supervisor 2026-10-19 (round 2): exclusions count toward the systematic rule, i.e. check's non-PASS kinds
            # (a FAIL, a non-normal class: freezes included), and verify's mechanics kinds (VERIFY_MECHANICS, levi);
            # verify's label-based and content-band kinds are reported per round with their share, never a stop
            for k in {reason_kind(x, coarse=True) for x in rs}:
                dest = kinds if f == "check" or k.startswith(VERIFY_MECHANICS) else reported
                dest.setdefault(plat, {}).setdefault(f, {}).setdefault(k, []).append(sd.name)
    share = {p: {f: {k: round(len(set(ss)) / n[p], 4) for k, ss in ks.items()} for f, ks in fk.items()}
             for p, fk in reported.items() if n.get(p)}
    return {"n_sessions": n, "kinds": kinds, "content": sorted(content),
            "reported": {"kinds": reported, "share": share,
                         "rule": "verify kinds: reported per round with their share, never a systematic stop"}}


def history_content(h: dict, sessions: list[Path]) -> set:
    """An earlier round's sessions read as content: its recorded list, or, for a round written before the list existed,
    each of its startup_failure sessions found beside this round's (the same roots) whose key never played normally
    before it (startup_failure_rule; that round's siblings unknown)."""
    rk = h.get("round_kinds") or {}
    if "content" in rk:
        return set(rk["content"])
    names = {x for fk in (rk.get("kinds") or {}).values() for ks in fk.values() for ss in ks.values() for x in ss}
    out = set()
    for name in names:
        for sd in sessions:
            sd = Path(sd).resolve()
            root = sd.parent.parent.parent if sd.parent.parent.name == "_failed" else sd.parent.parent
            for d in (root / sd.parent.name / name, root / "_failed" / sd.parent.name / name):
                sf = startup_failure_rule(d, []) if d.exists() else None
                if sf is not None and not sf["gates"]:
                    out.add(name)
    return out


def _without(rk: dict, drop: set) -> dict:
    """round_kinds without the sessions in `drop` (an earlier round's content sessions; counts reduced to match)."""
    if not drop:
        return rk
    kinds, n = {}, dict(rk.get("n_sessions") or {})
    for plat, fk in (rk.get("kinds") or {}).items():
        gone = set()
        for f, ks in fk.items():
            for k, ss in ks.items():
                keep = [x for x in ss if x not in drop]
                gone |= set(ss) - set(keep)
                if keep:
                    kinds.setdefault(plat, {}).setdefault(f, {})[k] = keep
        n[plat] = max(n.get(plat, 0) - len(gone), 0)
    return {**rk, "n_sessions": n, "kinds": kinds}


def systematic(cur: dict, history: list[dict], base: dict, hist_content: list[set] | None = None) -> list[str]:
    """Levi's systematic-exclusion stops (module docstring) for this round's kinds against the earlier rounds' (each
    without its content sessions, hist_content)."""
    bad = []
    hc = hist_content or [set()] * len(history)
    history = [{**h, "round_kinds": _without(h["round_kinds"], c)} for h, c in zip(history, hc)]
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


def _json(p: Path) -> dict:
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}


def _key(platform: str, meta: dict) -> str | None:
    if platform == "youtube":
        return meta.get("channel_handle") or None
    return (meta.get("channel_url") or "").split("?")[0].rstrip("/").lower() or None


def _roots(sd: Path) -> tuple[Path, list[Path]]:
    """(platform dir name, the accepted session dirs of that platform in both modes' roots) for a session dir."""
    sd = Path(sd).resolve()
    plat = sd.parent.name
    trio = next((a for a in sd.parents if a.name.startswith("_trio")), None)
    data = trio.parent if trio is not None else (sd.parent.parent.parent if sd.parent.parent.name == "_failed" else sd.parent.parent)
    dirs = [d for root in ([data] + ([trio] if trio is not None else [])) for d in (root / plat).glob("*")]
    return plat, dirs


def startup_failure_rule(sd: Path, round_sessions: list[Path]) -> dict | None:
    """levi's rule for a startup_failure session: {gates, played_before, siblings_failed, key}; None otherwise."""
    if _json(Path(sd) / "check.json").get("session_class") != "startup_failure":
        return None
    meta = _json(Path(sd) / "meta.json")
    plat, dirs = _roots(sd)
    key, t0 = _key(plat, meta), float(meta.get("started_at") or 0)
    played = []
    for d in dirs:
        if d.resolve() == Path(sd).resolve():
            continue
        c, m = _json(d / "check.json"), _json(d / "meta.json")
        if (key and c.get("verdict") == "PASS" and c.get("session_class") in ("normal", None)
                and _key(plat, m) == key and float(m.get("started_at") or 0) < t0):
            played.append(d.name)
    sib = [Path(x).name for x in round_sessions if Path(x).resolve() != Path(sd).resolve()
           and _json(Path(x) / "check.json").get("session_class") == "startup_failure"]
    # levi 2026-10-20 (tw_100016): a session whose collector logged a player error (an error page or the ad-wait
    # ceiling: meta.json player_errors) failed on mechanics, never content: it gates and is tallied
    perr = [e.get("reason") for e in meta.get("player_errors") or []]
    return {"key": key, "played_before": sorted(played), "siblings_failed": sib, "player_errors": perr,
            "gates": bool(played or sib or perr)}


def check(gate2: dict, teardown: dict | None, sessions: list[Path], history: list[dict] | None = None,
          base: dict | None = None) -> dict:
    rows = {Path(r.get("session", "")).name: r for r in gate2.get("sessions") or []}
    bad, report, content = [], {}, set()
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
        sf = startup_failure_rule(sd, sessions)
        if sf is not None:
            if sf["gates"]:
                b.append(f"startup_failure gates: played before {sf['played_before']} / siblings failed {sf['siblings_failed']}"
                         + (f" / player errors {sf['player_errors']}" if sf["player_errors"] else ""))
            else:                                          # levi: content, excluded; items 1 / 2 do not gate
                b = [x for x in b if not x.startswith(("item 1:", "item 2:"))]
                n.insert(0, f"startup_failure: content, excluded ({sf['key']} never played in either mode)")
                content.add(name)                          # nor does it count toward a systematic stop
        cov = coverage(sd)
        if not cov["ok"]:
            b.append(f"device record: {cov.get('why') or cov.get('covered_share')}")
        if "_failed" in sd.parts:
            n.insert(0, "in _failed (stops only on the reader's gating items)")
        bad += [f"{name} {x}" for x in b]
        report[name] = {"reported": n, "device_record": cov}
    rk = round_kinds(gate2, content)
    if base is not None:
        bad += systematic(rk, history or [], base, [history_content(h, sessions) for h in history or []])
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
