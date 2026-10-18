#!/usr/bin/env python3
"""t53 pilot-1 pre-check of the trio "composite" layout's code path on the collection display: two throwaway Chromes
(ports 9333 / 9334, /tmp profiles; never the batch Chrome on 9222), the second over the first, each with its own page
(fb_layout_check's per-instance pages); for the occluded one (9333) the collector's own functions run as a session
would: browser.calibrate_composite (the window by the browser pid, the viewport in the frame against its own CDP
capture) and three browser._composite_shot grabs 2 s apart (crop, staleness record). Passes when the calibration is
accepted, the frame it found belongs to 9333 (its pid), every grab returns data and the counter-driven pixels are not
stale. Prints JSON, saves the crops to /tmp/composite_check_*.jpg. Run from networkSim/ in a boundary pause:

    DISPLAY=:0 ../.venv/bin/python ../ops/diag/composite_check.py [--chrome google-chrome]
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chrome", default=shutil.which("google-chrome") or "google-chrome")
    a = ap.parse_args()
    out: dict = {"display": os.environ.get("DISPLAY")}
    procs = []
    try:
        for port in F.PORTS:
            procs.append(F.launch(a.chrome, port, 0, 1600, 900))
        pages = {p: F.page_of(p) for p in F.PORTS}
        time.sleep(3)
        try:
            pages[F.PORTS[1]][1].call("Page.bringToFront")
        except Exception:
            pass
        time.sleep(2)
        port = F.PORTS[0]
        B.CDP_BASE = session_meta.CDP_BASE = f"http://localhost:{port}"
        page = pages[port][1]
        out["browser"] = {k: v for k, v in session_meta.chrome_cmdline(B.CDP_BASE).items() if k != "argv"}
        origin = out["calibration"] = B.calibrate_composite(page)
        shots, prev = [], {}
        if origin.get("accepted"):
            for i in range(3):
                data, px, region, crop, rec = B._composite_shot(origin, None, page, prev)
                if data:
                    Path(f"/tmp/composite_check_{i}.jpg").write_bytes(data)
                shots.append({"bytes": len(data or b""), "px": px, "region": list(region), "crop": crop, **rec})
                time.sleep(2)
        out["shots"] = shots
        out["ok"] = bool(origin.get("accepted") and origin.get("browser_pid") == out["browser"].get("pid")
                         and len(shots) == 3 and all(s["bytes"] for s in shots)
                         and all(s["diff_prev"] is None or s["diff_prev"] > 0 for s in shots))
    except Exception as e:
        out["error"] = repr(e)[:400]
        out["ok"] = False
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    print(json.dumps(out, indent=1, default=str))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
