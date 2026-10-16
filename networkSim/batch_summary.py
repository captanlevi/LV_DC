#!/usr/bin/env python3
"""batch_summary.py: the collection v2 daily summary (LVQoE t37; the operator sends it to the supervisor).

Reads every session's check.json under data/<platform>/ (accepted) and data/_failed/<platform>/ (FAIL, archived) and
writes data/_summaries/summary_<YYYY-MM-DD>.md and .json for the sessions started that day (local time, meta.json
started_at), plus the cumulative totals:
  - sessions per platform: PASS / FAIL, FAIL reasons;
  - background: exogenous and all-background share per session (median / max), per-category shares (mean), and bytes
    per background host summed over the day's sessions (each session's top-12 hosts from check.json, so the long tail
    is cut);
  - class mix of accepted sessions: tier shares of quality time (ads masked), weighted by session duration; stall share;
  - sizes: pcap MB per session, total GB, disk free on the data volume;
  - yt channel rotation counts.
Nothing here gates anything.

Usage: python batch_summary.py [--date YYYY-MM-DD] [--data-dir ../data]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import shutil
import statistics
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLATFORMS = ("youtube", "twitch", "tiktok", "bilibili")


def _load(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def sessions(data_dir: Path) -> list[dict]:
    out = []
    for status, root in (("accepted", data_dir), ("failed", data_dir / "_failed")):
        for pf in PLATFORMS:
            for sd in sorted((root / pf).glob("*")) if (root / pf).exists() else []:
                if not sd.is_dir():
                    continue
                chk, meta = _load(sd / "check.json"), _load(sd / "meta.json") or {}
                started = meta.get("started_at")
                day = dt.datetime.fromtimestamp(started).date().isoformat() if started else None
                out.append({"session": f"{pf}/{sd.name}", "status": status, "platform": pf, "day": day,
                            "check": chk, "meta": meta,
                            "pcap_mb": (sd / "merged.pcap").stat().st_size / 1e6 if (sd / "merged.pcap").exists() else 0.0})
    return out


def _reason_key(r: str) -> str:
    """'capture gap: minutes with zero client packets [3]' -> 'capture gap'; numbers dropped so reasons aggregate."""
    head = r.split(":")[0].split("(")[0].strip()
    return " ".join(w for w in head.split() if not any(ch.isdigit() for ch in w))[:60] or r[:60]


def summarise(rows: list[dict]) -> dict:
    by_pf = collections.defaultdict(list)
    for r in rows:
        by_pf[r["platform"]].append(r)
    out = {}
    for pf, rs in sorted(by_pf.items()):
        checks = [r["check"] for r in rs if r["check"]]
        verdicts = collections.Counter((r["check"] or {}).get("verdict", "NO_CHECK") for r in rs)
        reasons = collections.Counter(_reason_key(x) for c in checks if c.get("verdict") == "FAIL" for x in c.get("reasons", []))
        tr = [c.get("traffic") or {} for c in checks]
        exo = [t["exogenous_share"] for t in tr if t.get("exogenous_share") is not None]
        bg = [t["background_share"] for t in tr if t.get("background_share") is not None]
        cats = collections.defaultdict(list)
        for t in tr:
            for k, v in (t.get("category_shares") or {}).items():
                cats[k].append(v)
        hosts = collections.Counter()
        for t in tr:
            for h in t.get("background_hosts_top") or []:
                hosts[(h["host"], h["category"])] += h.get("mb", 0.0)
        # class mix over accepted (PASS) sessions, weighted by duration
        mix, wsum, stall = collections.Counter(), 0.0, []
        for c in checks:
            if c.get("verdict") != "PASS":
                continue
            lab = c.get("labels") or {}
            dur = lab.get("duration_s") or 0.0
            for tier, share in (lab.get("tier_mix_quality_time") or {}).items():
                mix[tier] += share * dur
            wsum += dur
            if lab.get("stall_share") is not None:
                stall.append(lab["stall_share"])
        sizes = [r["pcap_mb"] for r in rs if r["pcap_mb"]]
        out[pf] = {
            "sessions": len(rs), "verdicts": dict(verdicts), "fail_reasons": dict(reasons.most_common()),
            "exogenous_share_median_max": [statistics.median(exo), max(exo)] if exo else None,
            "background_share_median_max": [statistics.median(bg), max(bg)] if bg else None,
            "category_share_mean": {k: round(sum(v) / len(v), 4) for k, v in sorted(cats.items())},
            "background_hosts_mb": [{"host": h, "category": c, "mb": round(mb, 2)} for (h, c), mb in hosts.most_common(15)],
            "class_mix_accepted": {k: round(v / wsum, 4) for k, v in sorted(mix.items(), key=lambda kv: int(kv[0]))} if wsum else {},
            "accepted_hours": round(wsum / 3600, 2),
            "stall_share_median": statistics.median(stall) if stall else None,
            "pcap_mb_median": statistics.median(sizes) if sizes else None, "pcap_gb_total": round(sum(sizes) / 1e3, 2),
        }
    return out


def rotation(rows: list[dict]) -> dict:
    c = collections.Counter(r["meta"].get("channel_handle") for r in rows
                            if r["platform"] == "youtube" and r["status"] == "accepted" and r["meta"].get("channel_handle"))
    return dict(sorted(c.items()))


def write(data_dir: Path = DATA_DIR, day: str | None = None) -> Path:
    day = day or dt.date.today().isoformat()
    rows = sessions(data_dir)
    du = shutil.disk_usage(data_dir)
    out = {"day": day, "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "disk": {"free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1), "free_share": round(du.free / du.total, 3)},
           "today": summarise([r for r in rows if r["day"] == day]),
           "cumulative": summarise(rows), "yt_channel_sessions_accepted": rotation(rows)}
    dest = data_dir / "_summaries"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"summary_{day}.json").write_text(json.dumps(out, indent=1, default=str))
    md = [f"# collection v2 summary {day} (generated {out['generated_at']})", "",
          f"disk free {out['disk']['free_gb']} GB of {out['disk']['total_gb']} GB ({out['disk']['free_share']})", ""]
    for scope in ("today", "cumulative"):
        md += [f"## {scope}", "", "| platform | sessions | verdicts | FAIL reasons | exogenous share median / max | "
               "background share median / max | accepted hours | class mix (accepted) | stall share median | pcap MB median | pcap GB |",
               "|---|---|---|---|---|---|---|---|---|---|---|"]
        for pf, v in out[scope].items():
            md.append(f"| {pf} | {v['sessions']} | {v['verdicts']} | {v['fail_reasons']} | {v['exogenous_share_median_max']} | "
                      f"{v['background_share_median_max']} | {v['accepted_hours']} | {v['class_mix_accepted']} | "
                      f"{v['stall_share_median']} | {v['pcap_mb_median']} | {v['pcap_gb_total']} |")
        md += ["", f"Background hosts ({scope}, MB summed over sessions' top-12):", ""]
        for pf, v in out[scope].items():
            md.append(f"- {pf}: " + ", ".join(f"{h['host']} ({h['category']}) {h['mb']}" for h in v["background_hosts_mb"][:10]))
        md.append("")
    md += ["## yt channel rotation (accepted sessions)", "", str(out["yt_channel_sessions_accepted"]), ""]
    (dest / f"summary_{day}.md").write_text("\n".join(md))
    return dest / f"summary_{day}.md"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=None)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    a = ap.parse_args()
    print(write(a.data_dir, a.date))
