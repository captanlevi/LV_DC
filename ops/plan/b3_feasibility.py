"""B3 (collector v3 spec; t68; interface 61a8bf36 signed by peter 18:02): the pre-window feasibility table. Per platform x
tier x geometry x DPR, whether any shaper band can carry the tier into the drawn player box, from the offered ladder
(A4 meta.offered_ladder), the matrix config's band rates (utils.shaping.SCENARIOS rate_min / rate_max) and the boxes.
A tier no cell can reach is a structural zero, never inferred from absence in labels. A planning record: orchestrate
never imports it; the launch script (rounds_ref_n, t69) runs it before orchestrate, writes b3_feasibility.json beside
the window's launch file and prints the per-tier table into the launch log.

A cell (platform, tier, geometry, dpr) is reachable iff it has no reasons:
- not_offered: no session of the previous window offered the tier (offered_share 0);
- above_tier_cap (yt only; tw / tt have no cap, c15): the tier is above geometry.tier_cap(name, dpr), B1's evidence
  table YT_TIER_CAP, the one cap steering uses (exp.py / steering.steer); the box (device px) stays in the cell as a
  record, never a rule (B1: the 855x481 box reached 720, which a box rule would forbid);
- no_tier_cap: yt (mode, DPR) with no YT_TIER_CAP entry: exp.py refuses such a session (B1 REFUSED), so nothing is reached;
- no_band_reaches_need: no band's rate_max x RATIO_LO >= need_kbit x HEADROOM.
band_share_reaching(state) = the share of [rate_min, rate_max] x RATIO_LO at or above the need (the walk spans the band).
expected_hours = planned counted hours x draw mass of the reachable cells x sum_s pi(s) P(tier | s) band_share(s): a
planning estimate (P from the coverage joint table, pooled over geometry), printed, never gated.

    python ops/plan/b3_feasibility.py --offers-metas <prev window meta.json ...> --window <run> --out b3_feasibility.json
        [--boxes boxes.json] [--joint coverage_joint.json] [--stationary pi.json] [--planned-hours H] [--dprs 1]"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "networkSim"))
from coverage_report import RUNGS, quality_class  # noqa: E402
from geometry import GEOMETRY_CHOICES, parse, tier_cap  # noqa: E402
from utils.shaping import SCENARIOS  # noqa: E402

SCHEMA = "b3_feasibility/1"
RATIO_LO = 0.9            # readers/shaper_math.RATIO_LO: the realised band's lower edge (levi efb5e4cc (1))
HEADROOM = 1.0            # interface 61a8bf36 (peter): the build default; levi may set it in the gate prereg
# levi 16:25 / 16:30 (2026-10-24, rushi's decision): shaper truth is out of the v3 gate and tw's realised-rate rule (19:31)
# is withdrawn. The primary cells ALWAYS plan on the configured band rates (x RATIO_LO); --realised adds a second set
# of columns beside them (*_realised), never a replacement.
TIER_SHORT = {"Q_144p": 144, "Q_240p": 240, "Q_360p": 360, "Q_480p": 480, "Q_720p": 720, "Q_1080p": 1080}
PLATFORMS = ("youtube", "twitch", "tiktok")


def _sha(path) -> str | None:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path and Path(path).is_file() else None


def offers(metas: list[dict], platform: str) -> tuple[dict, dict, str]:
    """(offered_share {tier: share of sessions offering it}, need_kbit {tier: median offered bitrate}, bitrate source).
    A tier's need falls back to the declared rung (coverage_report.RUNGS) when no offer carries a bitrate."""
    sess = [m for m in metas if m.get("platform") == platform and m.get("offered_ladder")]
    seen, rates = {}, {}
    for m in sess:
        ol = m["offered_ladder"]
        tiers = set()
        for rec in [ol] + list(ol.get("changes") or []):
            for r in rec.get("renditions") or []:
                t = quality_class(r.get("w") or 0, r.get("h") or 0)
                if t is None:
                    continue
                tiers.add(t)
                if r.get("bitrate_kbit") is not None:
                    rates.setdefault(t, []).append(float(r["bitrate_kbit"]))
        for t in tiers:
            seen[t] = seen.get(t, 0) + 1
    share = {t: seen[t] / len(sess) for t in seen} if sess else {}
    need, src = {}, "offer"
    for t in TIER_SHORT:
        if rates.get(t):
            need[t] = statistics.median(rates[t])
        elif t in RUNGS.get(platform, {}):
            need[t], src = RUNGS[platform][t][0], "offer+declared_rungs"
    return share, need, src if rates else "declared_rungs"


def band_edges(state: str, platform: str | None = None, realised: dict | None = None) -> tuple[float, float]:
    """The band's [lo, hi] effective kbit: configured x RATIO_LO, or for a REALISED platform configured x its state's
    realised ratio (realised[platform][state], shaper_realised's per-span median of delivered / applied)."""
    sc = SCENARIOS[state]
    f = ((realised or {}).get(platform) or {}).get(state) if platform else None
    k = RATIO_LO if f is None else float(f)
    return sc.rate_min * k, sc.rate_max * k


def band_share(state: str, need: float, platform: str | None = None, realised: dict | None = None) -> float:
    lo, hi = band_edges(state, platform, realised)
    req = need * HEADROOM
    if req <= lo:
        return 1.0
    if req > hi:
        return 0.0
    return (hi - req) / (hi - lo)


def box(platform: str, geom: str, dpr: float, boxes: dict) -> tuple[list[int], str]:
    """The player box in device px: the previous window's measured median (A7) when given, else the geometry's nominal
    size x DPR (flagged nominal; it overstates theater / window boxes, c14)."""
    m = (boxes.get(platform) or {}).get(f"{geom}@{dpr:g}")
    if m:
        return [int(m[0]), int(m[1])], "measured"
    _, w, h = parse(geom)
    return [int(round(w * dpr)), int(round(h * dpr))], "nominal"


def build(metas: list[dict], boxes: dict, dprs: list[float], joint: dict | None, stationary: dict | None,
          planned_hours: float, geometry: dict = GEOMETRY_CHOICES, realised: dict | None = None) -> dict:
    cells, per_tier, rate_src = [], [], {}
    for pf in PLATFORMS:
        rate_src[pf] = "configured x RATIO_LO" if geometry.get(pf) else "not built"
        rz = realised if (realised or {}).get(pf) and geometry.get(pf) else None   # the sensitivity columns, if asked
        if rz is not None:
            rate_src[pf] += " (+ realised sensitivity columns)"
        share, need, bsrc = offers(metas, pf)
        tiers = sorted({*share, *RUNGS.get(pf, {})}, key=lambda t: TIER_SHORT[t])
        geoms = geometry.get(pf, [])
        mass = 1.0 / (len(geoms) * len(dprs)) if geoms and dprs else 0.0     # equal draw weights (restart §7)
        pi = stationary.get(pf) if stationary else None
        pi = pi or {s: 1.0 / len(SCENARIOS) for s in SCENARIOS}
        by_state = ((joint or {}).get("per_platform", {}).get(pf) or {}).get("by_state") or {}
        for t in tiers:
            nk = need.get(t)
            reach_states = [s for s in SCENARIOS if nk is not None and band_edges(s, pf)[1] >= nk * HEADROOM]
            reach_states_rz = ([s for s in SCENARIOS if nk is not None and band_edges(s, pf, rz)[1] >= nk * HEADROOM]
                               if rz is not None else None)
            reach_cells, reach_cells_rz = [], []
            for g in geoms:
                for d in dprs:
                    bx, bsrc_box = box(pf, g, d, boxes)
                    reasons = []
                    if share.get(t, 0.0) == 0.0:
                        reasons.append("not_offered")
                    try:                                # B1's cap of record (None off yt); a missing entry refuses
                        cap = tier_cap(pf, g, d)
                    except ValueError:
                        reasons.append("no_tier_cap")
                    else:
                        if cap is not None and TIER_SHORT[t] > TIER_SHORT[cap]:
                            reasons.append("above_tier_cap")
                    if not reach_states:
                        reasons.append("no_band_reaches_need")
                    cell = {"platform": pf, "tier": t, "geometry": g, "dpr": d, "box_device_px": bx,
                            "box_source": bsrc_box, "offered_share": round(share.get(t, 0.0), 4),
                            "need_kbit": nk, "size_ok": not {"above_tier_cap", "no_tier_cap"} & set(reasons),
                            "rate_ok": bool(reach_states), "bands_reaching": reach_states,
                            "reachable": not reasons, "reasons": reasons}
                    if not reasons:
                        reach_cells.append(f"{g}@{d:g}")
                    if reach_states_rz is not None:     # the sensitivity: the same cell under the realised rates
                        other = [r for r in reasons if r != "no_band_reaches_need"]
                        cell.update(rate_ok_realised=bool(reach_states_rz), bands_reaching_realised=reach_states_rz,
                                    reachable_realised=not other and bool(reach_states_rz))
                        if cell["reachable_realised"]:
                            reach_cells_rz.append(f"{g}@{d:g}")
                    cells.append(cell)
            m_reach = mass * len(reach_cells)
            exp = 0.0
            for s, p in pi.items():
                row = by_state.get(s) or {}
                tot = sum(row.values())
                if tot and nk is not None:
                    exp += p * row.get(t, 0.0) / tot * band_share(s, nk, pf)
            rec = {"platform": pf, "tier": t, "structural_zero": not reach_cells,
                   "reachable_cells": reach_cells, "draw_mass_reachable": round(m_reach, 4),
                   "band_share_reaching": {s: round(band_share(s, nk, pf), 3) for s in SCENARIOS} if nk else {},
                   "rate_source": rate_src[pf],
                   "expected_hours_next_window": round(planned_hours * m_reach * exp, 3),
                   "bitrate_source": bsrc}
            if rz is not None:
                rec.update(structural_zero_realised=not reach_cells_rz, reachable_cells_realised=reach_cells_rz,
                           band_share_reaching_realised=({s: round(band_share(s, nk, pf, rz), 3) for s in SCENARIOS}
                                                         if nk else {}))
            per_tier.append(rec)
    for c in cells:                                  # levi's C5: no tier marked feasible with zero offered
        assert not (c["reachable"] and c["offered_share"] == 0.0), c
    return {"cells": cells, "per_tier": per_tier, "rate_source": rate_src}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offers-metas", nargs="+", type=Path, required=True)
    ap.add_argument("--window", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--boxes", type=Path, default=None, help="{platform: {'<geometry>@<dpr>': [w, h]}} measured (A7)")
    ap.add_argument("--joint", type=Path, default=None, help="coverage_joint.json (P(tier | state) for expected hours)")
    ap.add_argument("--stationary", type=Path, default=None, help="{platform: {state: probability}} planned")
    ap.add_argument("--planned-hours", type=float, default=0.0, help="planned counted hours per platform")
    ap.add_argument("--dprs", default="1", help="the window's DPR draw support (B2), e.g. 1 or 1,2")
    ap.add_argument("--realised", type=Path, default=None,
                    help="{platform: {state: realised ratio}} from shaper_realised: adds *_realised sensitivity columns beside the "
                         "configured cells, never replacing them (levi 16:25 / 16:30)")
    ap.add_argument("--code-sha", default=None)
    a = ap.parse_args(argv)
    metas = []
    for p in a.offers_metas:
        m = json.loads(p.read_text())
        m.setdefault("platform", p.parent.parent.name)
        metas.append(m)
    boxes = json.loads(a.boxes.read_text()) if a.boxes else {}
    joint = json.loads(a.joint.read_text()) if a.joint else None
    stat = json.loads(a.stationary.read_text()) if a.stationary else None
    dprs = [float(x) for x in a.dprs.split(",")]
    realised = json.loads(a.realised.read_text()) if a.realised else None
    res = build(metas, boxes, dprs, joint, stat, a.planned_hours, realised=realised)
    offers_sha = hashlib.sha256("".join(sorted(_sha(p) or "" for p in a.offers_metas)).encode()).hexdigest()
    out = {"schema": SCHEMA, "window": a.window, "built_at": time.time(), "code_sha": a.code_sha,
           "inputs": {"offers": {"sessions": len(metas), "sha256": offers_sha},
                      "state_table": {s: [SCENARIOS[s].rate_min, SCENARIOS[s].rate_max] for s in SCENARIOS},
                      "boxes_sha256": _sha(a.boxes), "realised_sha256": _sha(a.realised), "joint_sha256": _sha(a.joint), "stationary_sha256": _sha(a.stationary),
                      "dprs": dprs, "planned_hours": a.planned_hours},
           "rules": {"ratio_lo": RATIO_LO, "headroom": HEADROOM, "size_cap": "geometry.tier_cap (YT_TIER_CAP; B1)", "draw": "equal weights (restart §7)"},
           **res}
    a.out.write_text(json.dumps(out, indent=1) + "\n")
    for r in res["per_tier"]:                        # the per-tier table for the launch log
        print(f"B3 {r['platform']:8s} {r['tier']:8s} {'STRUCTURAL ZERO' if r['structural_zero'] else 'reachable':15s} "
              f"mass {r['draw_mass_reachable']:.3f} expected_h {r['expected_hours_next_window']:.3f} "
              f"cells {','.join(r['reachable_cells']) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
