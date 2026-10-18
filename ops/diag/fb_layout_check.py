#!/usr/bin/env python3
"""t53 pilot-1 layout probe v3 (design §4). On the collection display, in a boundary pause (no session running), with
throwaway Chromes only (ports 9333 / 9334, /tmp profiles; never the batch Chrome on 9222). The framebuffer and any
virtual monitor are restored at the end, also on error / Ctrl+C.

v2 read B as passing on a false positive (peter, 08:00): the two instances drew nearly the same page, so the occluded
window's `xwd -id` grab, which was the top window's pixels (its tab read fbprobe-9334, the under window was 9333),
matched the under window's capture and its counter moved. v3:
  - each instance draws its own page: tiles from a per-port pseudo-random sequence, the second instance in inverted
    grey, so one instance's capture never matches the other's pixels;
  - every grab is matched against BOTH instances' CDP captures; identity holds when its own capture is accepted and the
    other's is not (`identity`). The root grab is the positive control: it must read as the top window, never the
    other (`discriminator_ok`); without it the run's verdicts are void;
  - A' (the enlarged framebuffer, v1 / v2: mutter clamped the window back onto the monitor): `xrandr --fb 3W x H` plus
    two virtual monitors (`xrandr --setmonitor`) right of the real one, the instance moved onto the first;
  - B (overlapping windows): each window grabbed through XComposite (XCompositeNameWindowPixmap on its top-level frame,
    via ctypes: the redirected window's own pixmap) and by `xwd -id` (the control v2 used); identity and liveness (a
    per-frame counter box) per grab.
Prints JSON, saves the grabs to /tmp/fbprobe_*.png. Run from networkSim/ with the collector's venv:

    DISPLAY=:0 ../.venv/bin/python ../ops/diag/fb_layout_check.py [--chrome google-chrome] [--width 1920 --height 1080]
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
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
# a static high-contrast page per instance (the match needs structure; v3: tiles from a per-port LCG, so two instances
# share no pattern; the second instance inverted), a rAF frame counter and a per-instance title
PAGE_HTML = ("<title>fbprobe-{port}</title><body style='margin:0;background:#fff'>"
             "<canvas id=c width=1920 height=1200></canvas><script>"
             "var c=document.getElementById('c').getContext('2d'),s={seed}>>>0,inv={inv};"
             "for(var y=0;y<1200;y+=40)for(var x=0;x<1920;x+=40)"
             "{{s=(Math.imul(s,1103515245)+12345)>>>0;var d=((s>>>16)&1)^inv;c.fillStyle=d?'#111':'#eee';"
             "c.fillRect(x,y,40,40);}}"
             "c.font='120px monospace';c.fillStyle='#c00';c.fillText('{port}',200,300);"
             "window.__f=0;(function t(){{window.__f++;c.fillStyle='#fff';c.fillRect(1300,40,500,160);"
             "c.fillStyle='#000';c.font='140px monospace';c.fillText(String(window.__f%100000),1310,180);"
             "requestAnimationFrame(t)}})();</script>")
LIVE_BOX = (1300, 40, 500, 160)         # CSS px of the per-frame counter (dpr 1): a live grab changes there


def page_url(port: int) -> str:
    import urllib.parse
    i = PORTS.index(port) if port in PORTS else 0
    return "data:text/html," + urllib.parse.quote(PAGE_HTML.format(port=port, seed=port * 2654435761 % 2**32, inv=i % 2))


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


def identity(own: dict, other: dict) -> dict:
    """A grab is the instance's own pixels: its own capture accepted (match) and the other instance's not."""
    return {"own_accepted": bool(own.get("accepted")), "other_accepted": bool(other.get("accepted")),
            "own_residual": own.get("residual"), "other_residual": other.get("residual"),
            "ok": bool(own.get("accepted")) and not other.get("accepted")}


def match_both(caps: dict, grab, name: str, own: int) -> dict:
    """The grab matched against each instance's CDP capture (caps: port -> array), and the identity verdict."""
    other = next(p for p in caps if p != own)
    m = {str(p): match(caps[p], grab, f"{name}_vs_{p}") for p in caps}
    return {**m, "identity": identity(m[str(own)], m[str(other)])}


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


_c = ctypes


class _XImage(ctypes.Structure):
    _fields_ = [("width", _c.c_int), ("height", _c.c_int), ("xoffset", _c.c_int), ("format", _c.c_int),
                ("data", _c.POINTER(_c.c_ubyte)), ("byte_order", _c.c_int), ("bitmap_unit", _c.c_int),
                ("bitmap_bit_order", _c.c_int), ("bitmap_pad", _c.c_int), ("depth", _c.c_int),
                ("bytes_per_line", _c.c_int), ("bits_per_pixel", _c.c_int), ("red_mask", _c.c_ulong),
                ("green_mask", _c.c_ulong), ("blue_mask", _c.c_ulong)]


class _XErrorEvent(ctypes.Structure):
    _fields_ = [("type", _c.c_int), ("display", _c.c_void_p), ("resourceid", _c.c_ulong), ("serial", _c.c_ulong),
                ("error_code", _c.c_ubyte), ("request_code", _c.c_ubyte), ("minor_code", _c.c_ubyte)]


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [("x", _c.c_int), ("y", _c.c_int), ("width", _c.c_int), ("height", _c.c_int),
                ("border_width", _c.c_int), ("depth", _c.c_int), ("visual", _c.c_void_p), ("root", _c.c_ulong),
                ("class_", _c.c_int), ("bit_gravity", _c.c_int), ("win_gravity", _c.c_int), ("backing_store", _c.c_int),
                ("backing_planes", _c.c_ulong), ("backing_pixel", _c.c_ulong), ("save_under", _c.c_int),
                ("colormap", _c.c_ulong), ("map_installed", _c.c_int), ("map_state", _c.c_int),
                ("all_event_masks", _c.c_long), ("your_event_mask", _c.c_long), ("do_not_propagate_mask", _c.c_long),
                ("override_redirect", _c.c_int), ("screen", _c.c_void_p)]


def composite_grab(wid: str) -> tuple:
    """(grey array, record): the window's top-level frame (its ancestor under the root: the window the compositor
    redirects) read from its own pixmap (XCompositeNameWindowPixmap + XGetImage on the pixmap), not from the screen.
    X errors are caught (an error handler records them) and raised as RuntimeError."""
    import numpy as np
    from PIL import Image
    x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    xc = ctypes.CDLL(ctypes.util.find_library("Xcomposite") or "libXcomposite.so.1")
    errs: list[int] = []
    HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_XErrorEvent))
    handler = HANDLER(lambda d, e: errs.append(int(e.contents.error_code)) or 0)
    x11.XSetErrorHandler(handler)
    x11.XOpenDisplay.restype = ctypes.c_void_p
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        raise RuntimeError("XOpenDisplay failed")
    rec: dict = {}
    try:
        ev, er = ctypes.c_int(), ctypes.c_int()
        if not xc.XCompositeQueryExtension(ctypes.c_void_p(dpy), ctypes.byref(ev), ctypes.byref(er)):
            raise RuntimeError("no Composite extension")
        w = ctypes.c_ulong(int(wid, 16))
        root, parent, kids, n = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        chain = [hex(w.value)]
        while True:                                   # up to the child of the root (the WM frame)
            if not x11.XQueryTree(ctypes.c_void_p(dpy), w, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(kids),
                                  ctypes.byref(n)):
                raise RuntimeError("XQueryTree failed")
            if parent.value == root.value or parent.value == 0:
                break
            w = ctypes.c_ulong(parent.value)
            chain.append(hex(w.value))
        att = _XWindowAttributes()
        x11.XGetWindowAttributes(ctypes.c_void_p(dpy), w, ctypes.byref(att))
        rec.update(frame=hex(w.value), chain=chain, frame_size=[att.width, att.height], frame_depth=att.depth,
                   map_state=att.map_state)
        xc.XCompositeNameWindowPixmap.restype = ctypes.c_ulong
        pix = xc.XCompositeNameWindowPixmap(ctypes.c_void_p(dpy), w)
        x11.XSync(ctypes.c_void_p(dpy), 0)
        if errs or not pix:
            raise RuntimeError(f"NameWindowPixmap failed (X error {errs}): the frame is not redirected")
        x11.XGetImage.restype = ctypes.POINTER(_XImage)
        img = x11.XGetImage(ctypes.c_void_p(dpy), ctypes.c_ulong(pix), 0, 0, att.width, att.height,
                            ctypes.c_ulong(0xFFFFFFFF), 2)          # AllPlanes, ZPixmap
        x11.XSync(ctypes.c_void_p(dpy), 0)
        if errs or not img:
            raise RuntimeError(f"XGetImage on the pixmap failed (X error {errs})")
        im = img.contents
        rec.update(bits_per_pixel=im.bits_per_pixel, byte_order=im.byte_order, image_depth=im.depth)
        if im.bits_per_pixel not in (24, 32):
            raise RuntimeError(f"pixmap bits per pixel {im.bits_per_pixel}")
        buf = ctypes.string_at(im.data, im.bytes_per_line * im.height)
        raw = ("BGRX" if im.byte_order == 0 else "XRGB") if im.bits_per_pixel == 32 else ("BGR" if im.byte_order == 0 else "RGB")
        arr = np.asarray(Image.frombuffer("RGB", (im.width, im.height), buf, "raw", raw, im.bytes_per_line, 1)
                         .convert("L"), float)
        x11.XFreePixmap(ctypes.c_void_p(dpy), ctypes.c_ulong(pix))
        return arr, rec
    finally:
        x11.XCloseDisplay(ctypes.c_void_p(dpy))


def window_grab(wid: str):
    r = subprocess.run(["xwd", "-silent", "-id", wid], capture_output=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"xwd rc {r.returncode}: {r.stderr.decode(errors='ignore')[:200]}")
    return parse_xwd(r.stdout)


def live(wid: str, x_off: int, y_off: int, grab=None) -> dict:
    """Two grabs of the window 1 s apart: does the per-frame counter box change (the grab is the window's current
    content, not a stale copy)? x_off / y_off: the viewport's origin inside the grabbed image. Liveness alone does not
    say whose pixels they are (v2): identity does."""
    import numpy as np
    grab = grab or window_grab
    a = grab(wid)
    time.sleep(1.0)
    b = grab(wid)
    x, y, w, h = LIVE_BOX
    ra = a[y_off + y:y_off + y + h, x_off + x:x_off + x + w]
    rb = b[y_off + y:y_off + y + h, x_off + x:x_off + x + w]
    if ra.size == 0 or ra.shape != rb.shape:
        return {"error": "counter box outside the grab"}
    d = float(np.abs(ra - rb).mean())
    return {"counter_box_mean_abs_diff": round(d, 2), "live": d > 2.0}


def wm_name() -> str | None:
    rc, out, _ = sh(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"])
    m = re.search(r"window id # (0x[0-9a-f]+)", out)
    if not m:
        return None
    rc, out, _ = sh(["xprop", "-id", m.group(1), "_NET_WM_NAME"])
    m = re.search(r'= "(.*)"', out)
    return m.group(1) if m else None


def set_bounds(port: int, target: dict, bounds: dict) -> dict:
    B.CDP_BASE = f"http://localhost:{port}"
    wid = B._browser_call("Browser.getWindowForTarget", {"targetId": target["id"]}).get("windowId")
    B._browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
    B._browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": bounds})
    time.sleep(2)
    return B._browser_call("Browser.getWindowBounds", {"windowId": wid}).get("bounds")


def grab_checks(caps: dict, port: int, wid: str, name: str) -> dict:
    """Each window-grab method on the instance's window: identity against both captures and liveness."""
    recs: dict = {}

    def comp(w):
        arr, recs["composite"] = composite_grab(w)
        return arr
    out = {}
    for meth, fn in (("composite", comp), ("xwd", window_grab)):
        try:
            m = match_both(caps, fn(wid), f"{name}_{meth}", port)
            own = m[str(port)]
            if own.get("found_at"):
                m["live"] = live(wid, own["found_at"][0] - 64, own["found_at"][1] - 64, fn)
            if meth in recs:
                m["x"] = recs[meth]
            out[meth] = m
        except Exception as e:
            out[meth] = {"error": repr(e)[:300]}
    return out


def _grab_ok(g: dict | None) -> bool:
    return bool((g or {}).get("identity", {}).get("ok") and (g or {}).get("live", {}).get("live"))


def verdicts(out: dict) -> dict:
    """A': on the virtual monitor, the root grab there is the instance (identity) and live, rendering at > 20 fps.
    B (per method): the root grab names exactly one top window (the discriminator works), and both windows' grabs are
    their own pixels and live, the under one rendering at > 20 fps. Without the discriminator B is void (None)."""
    A, Bo = out.get("A") or {}, out.get("B") or {}
    a_ok = bool(A.get("placed_off_monitor") and _grab_ok(A.get("root_grab"))
                and (A.get("state") or {}).get("raf_fps", 0) > 20)
    disc = bool(Bo.get("discriminator_ok"))
    res = {"A_virtual_monitor": a_ok, "discriminator_ok": disc}
    for meth in ("composite", "xwd"):
        if not disc:
            res[f"B_{meth}"] = None
            continue
        ins = [Bo.get(str(p)) or {} for p in PORTS]
        under = next((x for x in ins if x.get("role") == "under"), {})
        res[f"B_{meth}"] = bool(all(_grab_ok((x.get("grabs") or {}).get(meth)) for x in ins)
                                and (under.get("state") or {}).get("raf_fps", 0) > 20)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chrome", default=shutil.which("google-chrome") or "google-chrome")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    a = ap.parse_args()
    W, H = a.width, a.height
    out: dict = {"probe": "v3", "display": os.environ.get("DISPLAY"),
                 "tools": {t: bool(shutil.which(t)) for t in ("xrandr", "xwininfo", "xwd", "xprop")}, "wm": wm_name()}
    orig = screen_size()
    out["screen_before"] = orig
    procs: list[subprocess.Popen] = []
    monitors: list[str] = []
    aw, ah = int(W * 5 / 6), int(H * 5 / 6)                # under mutter's auto-maximize size (06:57: 1920x1080 was maximized)
    try:
        for port in PORTS:                                  # both instances first: identity needs both captures
            procs.append(launch(a.chrome, port, 0, aw, ah))
        pages = {p: page_of(p) for p in PORTS}
        time.sleep(3)
        # A': the enlarged framebuffer with two virtual monitors right of the real one; instance 0 moved onto the first
        rc, _, err = sh(["xrandr", "--fb", f"{3 * W}x{H}"])
        A = out["A"] = {"xrandr_fb": {"rc": rc, "err": err, "screen_after": screen_size()}, "setmonitor": []}
        for i, name in enumerate(("LVDC-V1", "LVDC-V2")):
            rc, _, err = sh(["xrandr", "--setmonitor", name, f"{W}/{W * 254 // 960}x{H}/{H * 254 // 960}+{(i + 1) * W}+0",
                             "none"])
            A["setmonitor"].append({"name": name, "rc": rc, "err": err})
            if rc == 0:
                monitors.append(name)
        A["monitors"] = sh(["xrandr", "--listmonitors"])[1].strip().splitlines()
        time.sleep(2)
        t0, p0 = pages[PORTS[0]]
        try:
            A["after_move"] = set_bounds(PORTS[0], t0, {"left": W + 50, "top": 50, "width": aw, "height": ah})
        except Exception as e:
            A["after_move"] = {"error": repr(e)}
        A["state"] = st = state(PORTS[0], t0, p0)
        A["x_window"] = xw = x_window(f"fbprobe-{PORTS[0]}")
        win = st["window"]
        A["placed_off_monitor"] = (win.get("sx") or 0) >= W
        vx, vy = int(win.get("sx") or 0), int(win.get("sy") or 0) + int((win.get("oh") or 0) - (win.get("ih") or 0))
        box = (max(0, vx - 64), max(0, vy - 64), int(win.get("iw") or W) + 128, int(win.get("ih") or H) + 128)
        caps = {p: cdp_capture(pg) for p, (_, pg) in pages.items()}
        A["root_grab"] = rg = match_both(caps, root_grab(*box), "A_root", PORTS[0])
        if rg[str(PORTS[0])].get("found_at"):
            f = rg[str(PORTS[0])]["found_at"]
            rg["live"] = live("root", f[0] - 64, f[1] - 64, lambda _w: root_grab(*box))
        if xw:
            A["window_grabs"] = grab_checks(caps, PORTS[0], xw["id"], "A")
        for name in list(monitors):                         # back to the real monitor before B
            sh(["xrandr", "--delmonitor", name])
            monitors.remove(name)
        if orig:
            sh(["xrandr", "--fb", f"{orig[0]}x{orig[1]}"])
        # B: both at the monitor's origin, overlapping (instance 1 brought to the front); the root grab names the top one
        Bo = out["B"] = {}
        for p in PORTS:
            Bo.setdefault("bounds", {})[str(p)] = set_bounds(p, pages[p][0], {"left": 0, "top": 0, "width": aw, "height": ah})
        try:
            pages[PORTS[1]][1].call("Page.bringToFront")
        except Exception:
            pass
        time.sleep(2)
        caps = {p: cdp_capture(pg) for p, (_, pg) in pages.items()}
        Bo["root"] = {str(p): match(caps[p], root_grab(0, 0, W, H), f"B_root_vs_{p}") for p in PORTS}
        top = [p for p in PORTS if Bo["root"][str(p)].get("accepted")]
        Bo["top"] = top[0] if len(top) == 1 else None
        Bo["discriminator_ok"] = len(top) == 1
        for p in PORTS:
            t_, pg = pages[p]
            xw = x_window(f"fbprobe-{p}")
            Bo[str(p)] = {"role": None if Bo["top"] is None else ("over" if p == Bo["top"] else "under"),
                          "state": state(p, t_, pg), "x_window": xw,
                          "grabs": grab_checks(caps, p, xw["id"], f"B_{p}") if xw else None}
    except Exception as e:
        out["error"] = repr(e)[:400]
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for name in monitors:
            sh(["xrandr", "--delmonitor", name])
        if orig:
            rc, _, err = sh(["xrandr", "--fb", f"{orig[0]}x{orig[1]}"])
            out["restored"] = {"rc": rc, "err": err, "screen": screen_size(),
                               "monitors": sh(["xrandr", "--listmonitors"])[1].strip().splitlines()}
    out["verdict"] = v = verdicts(out)
    print(json.dumps(out, indent=1, default=str))
    return 0 if (v["A_virtual_monitor"] or v.get("B_composite") or v.get("B_xwd")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
