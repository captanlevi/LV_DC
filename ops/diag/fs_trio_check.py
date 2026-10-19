#!/usr/bin/env python3
"""t53 (a) probe v4: a fullscreen window over two composite-layout windows, before any trio session uses it. On the
collection display, between rounds, with throwaway Chromes only (ports 9333 / 9334 / 9335, /tmp profiles; never the
batch Chrome on 9222). Each instance draws its own page (fb_layout_check's per-port pages).

  A (9333), B (9334): normal windows at the origin, as the trio's composite layout; C (9335): fullscreen (CDP
  windowState fullscreen), the yt slot's stand-in. Three rounds of checks: initial; after a refill (B killed and
  relaunched: a sibling's new window maps); after the keeper's action (xcomposite.activate on C). Per round:
    - who is on top: xcomposite.window_at(screen centre) and the stacking order, mapped to A / B / C by each browser's
      pid (CDP SystemInfo) and _NET_WM_PID;
    - the root grab's identity: matched against A's, B's and C's CDP captures (exactly one accepted names the window
      the screen shows);
    - window_at_agrees: the cheap per-shot gate (window_at) names the same window as the pixels;
    - C's own XComposite pixmap (mutter unredirects a focused fullscreen window: an X error or stale pixels);
    - A and B while covered: their composite grabs' identity and liveness, rAF frames per second, visibilityState.
Verdict: keeper_restores_top (after the activate, C on top by pixels), gate_valid (window_at agrees with the pixels in
every round), covered_ok (A and B visible and rendering > 20 fps, their grabs their own and live, in every round).
Prints JSON; saves the grabs to /tmp/fs_trio_*.png. Run from networkSim/ in a boundary pause:

    DISPLAY=:0 ../.venv/bin/python ../ops/diag/fs_trio_check.py [--chrome google-chrome]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "networkSim"))
sys.path.insert(0, str(HERE))
import browser as B  # noqa: E402
import fb_layout_check as F  # noqa: E402
import session_meta  # noqa: E402
import xcomposite as X  # noqa: E402

PA, PB, PC = 9333, 9334, 9335


def pid_of(port: int) -> int | None:
    return session_meta.browser_pid_cdp(f"http://localhost:{port}")


def frames_of(ports) -> dict:
    """port -> {client, frame} of its browser's largest viewable window."""
    out = {}
    for port in ports:
        w = X.windows_of_pid(pid_of(port) or -1)
        out[port] = w[0] if w else None
    return out


def name_of(win: str | None, frames: dict) -> str | None:
    for port, w in frames.items():
        if w and win in (w["client"], w["frame"]):
            return {PA: "A", PB: "B", PC: "C"}[port]
    return None


def _grey(frame: str):
    import numpy as np
    return np.asarray(X.grab(frame)[0].convert("L"), float)


def check_round(pages: dict, W: int, H: int, tag: str) -> dict:
    caps = {p: F.cdp_capture(pg) for p, (_, pg) in pages.items()}
    frames = frames_of(pages)
    top = X.window_at(W // 2, H // 2)
    rg = {str(p): F.match(caps[p], F.root_grab(0, 0, W, H), f"fs_trio_{tag}_root_vs_{p}") for p in pages}
    shown = [p for p in pages if rg[str(p)].get("accepted")]
    rec = {"window_at": top, "window_at_names": name_of(top, frames),
           "stacking_names": [name_of(w, frames) for w in X.stacking()],
           "root": rg, "root_shows": {PA: "A", PB: "B", PC: "C"}.get(shown[0]) if len(shown) == 1 else None}
    rec["window_at_agrees"] = rec["root_shows"] is not None and rec["root_shows"] == rec["window_at_names"]
    try:                                               # C's own pixmap: unredirected while fullscreen and focused?
        rec["c_pixmap"] = F.match(caps[PC], _grey(frames[PC]["frame"]), f"fs_trio_{tag}_c_pixmap")
    except Exception as e:
        rec["c_pixmap"] = {"error": repr(e)[:200]}
    for p in (PA, PB):
        w = frames.get(p)
        st = F.state(p, *pages[p])
        g = {}
        if w:
            try:
                g = F.match_both({q: caps[q] for q in (PA, PB)}, _grey(w["frame"]), f"fs_trio_{tag}_{p}", p)
                own = g[str(p)]
                if own.get("found_at"):
                    g["live"] = F.live(w["frame"], own["found_at"][0] - 64, own["found_at"][1] - 64, _grey)
            except Exception as e:
                g = {"error": repr(e)[:200]}
        rec[{PA: "A", PB: "B"}[p]] = {"raf_fps": st.get("raf_fps"), "visibility": st.get("visibility"), "grab": g}
    return rec


def covered_ok(r: dict) -> bool:
    return all((r.get(k) or {}).get("raf_fps", 0) > 20 and (r.get(k) or {}).get("visibility") == "visible"
               and ((r.get(k) or {}).get("grab") or {}).get("identity", {}).get("ok")
               and (((r.get(k) or {}).get("grab") or {}).get("live") or {}).get("live") for k in ("A", "B"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chrome", default=shutil.which("google-chrome") or "google-chrome")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    a = ap.parse_args()
    W, H = a.width, a.height
    out: dict = {"probe": "v4 (fullscreen in trio)", "display": os.environ.get("DISPLAY"), "wm": F.wm_name()}
    procs: dict = {}
    try:
        for port in (PA, PB, PC):
            procs[port] = F.launch(a.chrome, port, 0, 1600, 900)
        pages = {p: F.page_of(p) for p in (PA, PB, PC)}
        time.sleep(3)
        B.CDP_BASE = f"http://localhost:{PC}"
        wid = B._browser_call("Browser.getWindowForTarget", {"targetId": pages[PC][0]["id"]}).get("windowId")
        B._browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "fullscreen"}})
        time.sleep(3)
        out["c_window_state"] = B._browser_call("Browser.getWindowBounds", {"windowId": wid}).get("bounds")
        out["initial"] = check_round(pages, W, H, "initial")
        # a refill: B's browser ends and a new one maps (as a sibling slot's next session would)
        os.killpg(procs[PB].pid, signal.SIGTERM)
        time.sleep(2)
        procs[PB] = F.launch(a.chrome, PB, 0, 1600, 900)
        pages[PB] = F.page_of(PB)
        time.sleep(3)
        out["after_refill"] = check_round(pages, W, H, "after_refill")
        c = frames_of([PC])[PC]
        out["activate_sent"] = X.activate(c["client"]) if c else None
        time.sleep(2)
        out["after_activate"] = check_round(pages, W, H, "after_activate")
        rounds = [out[k] for k in ("initial", "after_refill", "after_activate")]
        out["verdict"] = {"keeper_restores_top": out["after_activate"]["root_shows"] == "C",
                          "gate_valid": all(r["window_at_agrees"] for r in rounds),
                          "covered_ok": all(covered_ok(r) for r in rounds),
                          "c_on_top_after_refill": out["after_refill"]["root_shows"] == "C"}
    except Exception as e:
        out["error"] = repr(e)[:400]
    finally:
        for p in procs.values():
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    print(json.dumps(out, indent=1, default=str))
    v = out.get("verdict") or {}
    return 0 if (v.get("keeper_restores_top") and v.get("gate_valid") and v.get("covered_ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
