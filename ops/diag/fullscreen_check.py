#!/usr/bin/env python3
"""Live check of the fullscreen hot-fix (t37) on a throwaway Chrome, never the batch Chrome (port 9222).

    # 1. a throwaway Chrome on the X display (port 9333, a /tmp profile), e.g.
    google-chrome --remote-debugging-port=9333 --remote-allow-origins='*' --user-data-dir=/tmp/fscheck_profile \
        --no-first-run --autoplay-policy=no-user-gesture-required --mute-audio about:blank &
    # 2. this script (from networkSim's directory, with the collector's venv)
    python3 ../ops/diag/fullscreen_check.py [--port 9333] [--url https://www.youtube.com/@LofiGirl/live]

Prints, as JSON: the window and player fullscreen record (window_fullscreen + yt_fullscreen), the shot-origin
calibration (calibrate_shot_origin: fullscreen_screen must be true, offset [0, 0]), and the X11 shot region the
loop would use; saves that region's grab to /tmp/fscheck_region.jpg for the eye check. Nothing is written elsewhere."""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "networkSim"))
import browser as B  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--url", default="https://www.youtube.com/@LofiGirl/live")
    a = ap.parse_args()
    if a.port == 9222:
        raise SystemExit("never the batch Chrome (9222): use a throwaway Chrome on 9333 / 9334")
    B.CDP_BASE = f"http://localhost:{a.port}"
    targets = json.loads(urllib.request.urlopen(f"{B.CDP_BASE}/json/list", timeout=5).read())
    t = next(x for x in targets if x.get("type") == "page")
    page = B.CDPPage(t["webSocketDebuggerUrl"])
    page.navigate(a.url)
    for _ in range(60):
        time.sleep(1)
        st = json.loads(page.eval("JSON.stringify({t: (document.querySelector('#movie_player video')||{}).currentTime||0})") or "{}")
        if st.get("t", 0) > 3:
            break
    out = {"playing_t": st.get("t")}
    out["fullscreen"] = B.yt_fullscreen(page, target_id=t["id"])
    out["origin"] = B.calibrate_shot_origin(page)
    pr = json.loads(page.eval(B._PLAYER_RECT_JS) or "{}")
    fs = B._fullscreen_on_screen(pr) if out["origin"].get("fullscreen_screen") else None
    vp = B._viewport_on_screen(json.loads(page.eval(B._VIEWPORT_JS) or "{}"))
    region = fs or vp
    out["shot_region"] = {"region": region, "source": "screen (calibrated)" if fs else "window model"}
    if region:
        data = B._grab_x11(region)
        if data:
            Path("/tmp/fscheck_region.jpg").write_bytes(data)
            out["shot_region"]["saved"] = "/tmp/fscheck_region.jpg"
    ok = bool((out["fullscreen"].get("window") or {}).get("ok")) and out["fullscreen"].get("on_screen") \
        and out["origin"].get("fullscreen_screen")
    out["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps(out, indent=1, default=str))
    page.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
