#!/usr/bin/env python3
"""t53 / t56 round preflight (supervisor, levi 2026-10-19): run at every counted round's start, before orchestrate. A
dirty box means no round (exit 1). Read-only; it changes nothing.

  clean box   the capture interface carries no htb / netem / ingress qdisc (the trio's shared tree is down); no lv* link
              (lvi_ / lvo_: no slot left up); no `lvdc_trio` nft table; no process in lvdc.slice; no Chrome with a
              remote-debugging port and no tcpdump running
  offloads    the capture interface's coalescing / segmentation offloads read off (t55; offloads.read)
  sha         the worktree is at the expected collector sha with no tracked change

    python3 ops/round_preflight.py --sha <collection sha> [--iface wlp0s20f3] [--worktree .] [--out preflight.json]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "networkSim"))
import offloads  # noqa: E402


def _run(cmd: list[str], run=subprocess.run) -> tuple[int, str]:
    try:
        r = run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, repr(e)


def check(iface: str, sha: str, worktree: Path, run=subprocess.run, cg_root: Path | None = None) -> dict:
    import trio_slots as T                     # imported here: single mode's modules never load trio_slots (test guard)
    cg_root = cg_root or T.CG_ROOT
    rec: dict = {"iface": iface, "expected_sha": sha}
    rc, qd = _run(["tc", "qdisc", "show", "dev", iface], run)
    bad_q = [line for line in qd.splitlines() if any(k in line for k in ("htb", "netem", "ingress"))]
    rec["qdisc"] = {"rc": rc, "shaping_left": bad_q, "ok": rc == 0 and not bad_q}
    rc, links = _run(["ip", "-o", "link", "show"], run)
    names = [ln.split(":")[1].strip().split("@")[0] for ln in links.splitlines() if ln.count(":") >= 2]
    lv = [n for n in names if n.startswith(("lvi_", "lvo_"))]
    rec["links"] = {"rc": rc, "lv_links": lv, "ok": rc == 0 and not lv}
    rc, nft = _run(["sudo", "-n", "nft", "list", "tables"], run)
    rec["nft"] = {"rc": rc, "lvdc_table": T.NFT_TABLE in nft, "ok": rc == 0 and T.NFT_TABLE not in nft}
    procs = []
    sl = cg_root / "lvdc.slice"
    if sl.exists():
        for f in sl.rglob("cgroup.procs"):
            try:
                procs += [p for p in f.read_text().split() if p]
            except OSError:
                pass
    rec["cgroup"] = {"slice_exists": sl.exists(), "procs": procs[:20], "ok": not procs}
    rc, ps = _run(["ps", "-eo", "pid,args"], run)
    stray = [ln.strip()[:160] for ln in ps.splitlines()
             if ("remote-debugging-port=" in ln and "chrome" in ln.lower()) or " tcpdump " in f" {ln} "]
    rec["processes"] = {"rc": rc, "stray": stray[:20], "ok": rc == 0 and not stray}
    off = offloads.read(iface, run)
    rec["offloads"] = {"features": off["features"], "ok": off["all_off"]}
    rc, head = _run(["git", "-C", str(worktree), "rev-parse", "HEAD"], run)
    rc2, st = _run(["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=no"], run)
    head = head.strip()
    rec["sha"] = {"head": head, "tracked_changes": [x for x in st.splitlines() if x.strip()][:20],
                  "ok": rc == 0 and rc2 == 0 and head.startswith(sha) and not st.strip()}
    rec["ok"] = all(v["ok"] for k, v in rec.items() if isinstance(v, dict))
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sha", required=True, help="the collection sha the round must run at (a prefix is enough)")
    ap.add_argument("--iface", default=offloads.IFACE_DEFAULT)
    ap.add_argument("--worktree", type=Path, default=HERE.parent)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    rec = check(a.iface, a.sha, a.worktree)
    text = json.dumps(rec, indent=1)
    if a.out:
        a.out.write_text(text)
    print(text)
    if not rec["ok"]:
        print("ROUND PREFLIGHT FAILED: " + ", ".join(k for k, v in rec.items() if isinstance(v, dict) and not v["ok"])
              + " -- no round", file=sys.stderr)
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
