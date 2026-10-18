#!/usr/bin/env python3
"""t53 pilot-1 layout probe (design §4: non-overlapping windows on an X framebuffer enlarged with `xrandr --fb`, X11
crops of each). On the collection display, in a boundary pause, with throwaway Chromes only (ports 9333 / 9334, /tmp
profiles; never the batch Chrome on 9222). The framebuffer is restored at the end, also on error / Ctrl+C.

  A. enlarged framebuffer: `xrandr --fb <3 x W>x<H>`, a Chrome launched at --window-position=W,0 (off the monitor):
     where the window manager put it (CDP bounds, window.screenX, xwininfo), whether it renders there (rAF frames per
     second, visibilityState), and whether an X11 grab of that root region is the page (CDP capture matched against it:
     browser.match_offset residual / gap / offset, as the shot calibration does);
  B. overlapping windows (the fallback if A fails): two Chromes at the same position; each window grabbed by its own
     id (`xwd -id`: with a compositing window manager a redirected window's contents are readable when occluded),
     matched against its own CDP capture; and the root grab, which shows only the top one.
Prints JSON, saves the grabs to /tmp/fbprobe_*.png. Run from networkSim/ with the collector's venv:

    DISPLAY=:0 ../.venv/bin/python ../ops/diag/fb_layout_check.py [--chrome google-chrome] [--width 1920 --height 1080]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "networkSim"))
import browser as B  # noqa: E402

PORTS = (9333, 9334)
# a static high-contrast page (the match needs structure) with a rAF frame counter and a per-instance title
PAGE_HTML = ("<title>fbprobe-{port}</title><body style='margin:0;background:#fff'>"
             "<canvas id=c width=1920 height=1200></canvas><script>"
             "var c=document.getElementById('c').getContext('2d');for(var y=0;y<1200;y+=40)for(var x=0;x<1920;x+=40)"
             "{{c.fillStyle=((x*7+y*13+{port})%97<48)?'#111':'#eee';c.fillRect(x,y,40,40);}}"
             "c.font='120px monospace';c.fillStyle='#c00';c.fillText('{port}',200,300);"
             "window.__f=0;(function t(){{window.__f++;c.fillStyle='#fff';c.fillRect(1300,40,500,160);"
             "c.fillStyle='#000';c.font='140px monospace';c.fillText(String(window.__f%100000),1310,180);"
             "requestAnimationFrame(t)}})();</script>")
LIVE_BOX = (1300, 40, 500, 160)         # CSS px of the per-frame counter (dpr 1): a live grab changes there


def page_url(port: int) -> str:
    import urllib.parse
    return "data:text/html," + urllib.parse.quote(PAGE_HTML.format(port=port))


def sh(cmd: list[str], timeout: float = 15) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr.strip()[:300]


def screen_size() -> tuple[int, int] | None:
    rc, out, _ = sh(["xrandr", "--current"])
    m = re.search(r"current (\d+) x (\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def launch(chrome: str, port: int, x: int, w: int, h: int) -> subprocess.Popen:
    if port == 9222:
        raise SystemExit("never the batch Chrome (9222)")
    prof = Path(f"/tmp/fbprobe_profile_{port}")
    args = [chrome, f"--remote-debugging-port={port}", "--remote-allow-origins=*", f"--user-data-dir={prof}",
            "--no-first-run", "--no-default-browser-check", "--disable-sync", "--mute-audio",
            "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling", f"--window-position={x},0", f"--window-size={w},{h}",
            page_url(port)]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)


def page_of(port: int, wait_s: float = 20):
    base = f"http://localhost:{port}"
    t_end = time.time() + wait_s
    while time.time() < t_end:
        try:
            ts = json.loads(urllib.request.urlopen(f"{base}/json/list", timeout=2).read())
            t = next((x for x in ts if x.get("type") == "page" and "fbprobe" in (x.get("title") or x.get("url") or "")), None)
            if t:
                return t, B.CDPPage(t["webSocketDebuggerUrl"])
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"no probe page on {port}")


def state(port: int, target: dict, page) -> dict:
    B.CDP_BASE = f"http://localhost:{port}"
    rec = {}
    try:
        wid = B._browser_call("Browser.getWindowForTarget", {"targetId": target["id"]}).get("windowId")
        rec["cdp_bounds"] = B._browser_call("Browser.getWindowBounds", {"windowId": wid}).get("bounds")
    except Exception as e:
        rec["cdp_bounds"] = {"error": repr(e)}
    js = ("JSON.stringify({sx: screenX, sy: screenY, iw: innerWidth, ih: innerHeight, ow: outerWidth, oh: outerHeight, "
          "sw: screen.width, sh: screen.height, aw: screen.availWidth, vis: document.visibilityState, f: window.__f, "
          "dpr: devicePixelRatio})")
    a = json.loads(page.eval(js) or "{}")
    time.sleep(3)
    b = json.loads(page.eval(js) or "{}")
    rec.update(window=a, raf_fps=round(((b.get("f") or 0) - (a.get("f") or 0)) / 3.0, 1), visibility=b.get("vis"))
    return rec


def cdp_capture(page):
    import numpy as np
    from PIL import Image
    shot = page.call("Page.captureScreenshot", {"format": "png", "fromSurface": True, "captureBeyondViewport": False})
    return np.asarray(Image.open(io.BytesIO(base64.b64decode(shot.get("data") or ""))).convert("L"), float)


def match(cdp, area, name: str) -> dict:
    """The CDP viewport (less a 64 px margin) found in the X11 image: B.match_offset's (y, x, residual, gap)."""
    import numpy as np
    from PIL import Image
    Image.fromarray(np.clip(area, 0, 255).astype("uint8")).save(f"/tmp/fbprobe_{name}.png")
    M = 64
    tpl = cdp[M:cdp.shape[0] - M, M:cdp.shape[1] - M]
    if area.shape[0] < tpl.shape[0] or area.shape[1] < tpl.shape[1]:
        return {"error": f"grab {area.shape} smaller than the template {tpl.shape}", "saved": f"/tmp/fbprobe_{name}.png"}
    y, x, res, gap = B.match_offset(tpl, area)
    return {"found_at": [x, y], "residual": res, "gap": gap, "accepted": B.calibration_verdict(res, gap),
            "grab_px": [area.shape[1], area.shape[0]], "saved": f"/tmp/fbprobe_{name}.png"}


def root_grab(x: int, y: int, w: int, h: int):
    import numpy as np
    from PIL import ImageGrab
    return np.asarray(ImageGrab.grab(bbox=(x, y, x + w, y + h), xdisplay=os.environ.get("DISPLAY") or ":0").convert("L"), float)


def x_window(title: str) -> dict | None:
    """The X window whose name starts with `title` (xwininfo -root -tree): id, size and absolute position."""
    rc, out, _ = sh(["xwininfo", "-root", "-tree"])
    for line in out.splitlines():
        m = re.search(r"(0x[0-9a-f]+) \"" + re.escape(title) + r"[^\"]*\".*?(\d+)x(\d+)\+-?\d+\+-?\d+\s+\+(-?\d+)\+(-?\d+)", line)
        if m:
            return {"id": m.group(1), "size": [int(m.group(2)), int(m.group(3))], "abs": [int(m.group(4)), int(m.group(5))]}
    return None


def parse_xwd(data: bytes):
    """An XWD dump (TrueColor ZPixmap, 24 / 32 bpp) as a grey float array."""
    import numpy as np
    from PIL import Image
    hdr = struct.unpack(">25I", data[:100])
    hsize, w, h, byte_order, bpp, bpl, ncolors = hdr[0], hdr[4], hdr[5], hdr[7], hdr[11], hdr[12], hdr[19]
    off = hsize + ncolors * 12
    if bpp == 32:
        raw = "BGRX" if byte_order == 0 else "XRGB"
    elif bpp == 24:                                   # a 24-bit visual (shagun: the occluded window, 06:57)
        raw = "BGR" if byte_order == 0 else "RGB"
    else:
        raise ValueError(f"xwd bits per pixel {bpp} (24 / 32 supported)")
    im = Image.frombuffer("RGB", (w, h), data[off:off + bpl * h], "raw", raw, bpl, 1)
    return np.asarray(im.convert("L"), float)


def window_grab(wid: str):
    r = subprocess.run(["xwd", "-silent", "-id", wid], capture_output=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"xwd rc {r.returncode}: {r.stderr.decode(errors='ignore')[:200]}")
    return parse_xwd(r.stdout)


def live(wid: str, x_off: int, y_off: int) -> dict:
    """Two grabs of the window 1 s apart: does the per-frame counter box change (the grab is the window's current
    content, not a stale or obscured copy)? x_off / y_off: the viewport's origin inside the grabbed window."""
    import numpy as np
    a = window_grab(wid)
    time.sleep(1.0)
    b = window_grab(wid)
    x, y, w, h = LIVE_BOX
    ra = a[y_off + y:y_off + y + h, x_off + x:x_off + x + w]
    rb = b[y_off + y:y_off + y + h, x_off + x:x_off + x + w]
    if ra.size == 0 or ra.shape != rb.shape:
        return {"error": "counter box outside the grab"}
    d = float(np.abs(ra - rb).mean())
    return {"counter_box_mean_abs_diff": round(d, 2), "live": d > 2.0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chrome", default=shutil.which("google-chrome") or "google-chrome")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    a = ap.parse_args()
    W, H = a.width, a.height
    out: dict = {"display": os.environ.get("DISPLAY"), "tools": {t: bool(shutil.which(t)) for t in ("xrandr", "xwininfo", "xwd")}}
    orig = screen_size()
    out["screen_before"] = orig
    procs: list[subprocess.Popen] = []
    try:
        # A: the enlarged framebuffer, one Chrome off the monitor
        rc, _, err = sh(["xrandr", "--fb", f"{3 * W}x{H}"])
        out["A"] = {"xrandr_fb": {"rc": rc, "err": err, "screen_after": screen_size()}}
        aw, ah = int(W * 5 / 6), int(H * 5 / 6)            # under mutter's auto-maximize size (06:57: 1920x1080 was maximized)
        procs.append(launch(a.chrome, PORTS[0], W, aw, ah))
        t0, p0 = page_of(PORTS[0])
        time.sleep(3)
        B.CDP_BASE = f"http://localhost:{PORTS[0]}"
        try:                                              # the launch position aside, an explicit move off the monitor
            wid0 = B._browser_call("Browser.getWindowForTarget", {"targetId": t0["id"]}).get("windowId")
            B._browser_call("Browser.setWindowBounds", {"windowId": wid0, "bounds": {"windowState": "normal"}})
            B._browser_call("Browser.setWindowBounds", {"windowId": wid0, "bounds": {"left": W + 50, "top": 50,
                                                                                     "width": aw, "height": ah}})
            time.sleep(2)
            out["A"]["after_move"] = B._browser_call("Browser.getWindowBounds", {"windowId": wid0}).get("bounds")
        except Exception as e:
            out["A"]["after_move"] = {"error": repr(e)}
        out["A"]["state"] = st = state(PORTS[0], t0, p0)
        out["A"]["x_window"] = xw = x_window(f"fbprobe-{PORTS[0]}")
        win = st["window"]
        vx, vy = int(win.get("sx") or 0), int(win.get("sy") or 0) + int((win.get("oh") or 0) - (win.get("ih") or 0))
        out["A"]["placed_off_monitor"] = (win.get("sx") or 0) >= W
        out["A"]["root_grab_vs_cdp"] = match(cdp_capture(p0), root_grab(max(0, vx - 64), max(0, vy - 64),
                                                                         int(win.get("iw") or W) + 128, int(win.get("ih") or H) + 128), "A_root")
        if xw and out["tools"]["xwd"]:
            try:
                m = out["A"]["window_grab_vs_cdp"] = match(cdp_capture(p0), window_grab(xw["id"]), "A_window")
                if m.get("found_at"):
                    out["A"]["window_live"] = live(xw["id"], m["found_at"][0] - 64, m["found_at"][1] - 64)
            except Exception as e:
                out["A"]["window_grab_vs_cdp"] = {"error": repr(e)}
        # B: a second Chrome over the first at the monitor's origin; each grabbed by its window id
        B.CDP_BASE = f"http://localhost:{PORTS[0]}"
        wid0 = B._browser_call("Browser.getWindowForTarget", {"targetId": t0["id"]}).get("windowId")
        B._browser_call("Browser.setWindowBounds", {"windowId": wid0, "bounds": {"left": 0, "top": 0, "width": W, "height": H}})
        procs.append(launch(a.chrome, PORTS[1], 0, W, H))
        t1, p1 = page_of(PORTS[1])
        time.sleep(3)
        out["B"] = {"under": state(PORTS[0], t0, p0), "over": state(PORTS[1], t1, p1)}
        for name, port, pg in (("under", PORTS[0], p0), ("over", PORTS[1], p1)):
            xw = x_window(f"fbprobe-{port}")
            out["B"][f"{name}_x_window"] = xw
            if xw and out["tools"]["xwd"]:
                try:
                    m = out["B"][f"{name}_window_grab_vs_cdp"] = match(cdp_capture(pg), window_grab(xw["id"]),
                                                                        f"B_{name}_window")
                    if m.get("found_at"):
                        out["B"][f"{name}_window_live"] = live(xw["id"], m["found_at"][0] - 64, m["found_at"][1] - 64)
                except Exception as e:
                    out["B"][f"{name}_window_grab_vs_cdp"] = {"error": repr(e)}
        out["B"]["root_grab_vs_under_cdp"] = match(cdp_capture(p0), root_grab(0, 0, W, H), "B_root")
    except Exception as e:
        out["error"] = repr(e)[:400]
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if orig:
            rc, _, err = sh(["xrandr", "--fb", f"{orig[0]}x{orig[1]}"])
            out["restored"] = {"rc": rc, "err": err, "screen": screen_size()}
    a_ok = bool(out.get("A", {}).get("placed_off_monitor")) and (out.get("A", {}).get("root_grab_vs_cdp") or {}).get("accepted") \
        and (out.get("A", {}).get("window_live") or {}).get("live") \
        and (out.get("A", {}).get("state") or {}).get("raf_fps", 0) > 20
    b_ok = all((out.get("B", {}).get(f"{n}_window_grab_vs_cdp") or {}).get("accepted")
               and (out.get("B", {}).get(f"{n}_window_live") or {}).get("live") for n in ("under", "over")) \
        and (out.get("B", {}).get("under") or {}).get("raf_fps", 0) > 20
    out["verdict"] = {"A_enlarged_framebuffer": bool(a_ok), "B_overlap_window_grab": bool(b_ok)}
    print(json.dumps(out, indent=1, default=str))
    return 0 if (a_ok or b_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
