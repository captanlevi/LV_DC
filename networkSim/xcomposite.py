"""X11 per-window capture through the Composite extension (t53 pilot-1 layout B, probe v3 at 8d0270c: an occluded
window's own pixmap matched its CDP capture, own residual about 1 against about 100 for the other window's).

Under a compositing window manager (GNOME Shell on chaos) every top-level frame is redirected: its contents live in an
off-screen pixmap, current whether or not another window covers it. XCompositeNameWindowPixmap names that pixmap and
XGetImage reads it. A screen grab (XGetImage on the root or on an unredirected window) shows whatever is on top instead:
probe v2's false positive. ctypes against libX11 / libXcomposite only (no new dependency); X errors are caught by an
error handler (Xlib's default handler exits the process) and raised as XError.

  windows_of_pid(pid)  the viewable client windows whose _NET_WM_PID is pid, with their top-level frames
  grab(frame)          (PIL RGB image, record) of a top-level frame's own pixmap
  window_at(x, y)      the top-level window shown at a root point (XTranslateCoordinates: the topmost mapped child)
  activate(client)     ask the window manager to activate and raise a window (_NET_ACTIVE_WINDOW, as a pager)
"""
from __future__ import annotations

import ctypes
import ctypes.util

_c = ctypes


class XError(RuntimeError):
    pass


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


IS_VIEWABLE, Z_PIXMAP, XA_CARDINAL = 2, 2, 6
_ERRS: list[int] = []
_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_XErrorEvent))(
    lambda d, e: _ERRS.append(int(e.contents.error_code)) or 0)
_LIBS: dict = {}


def _libs():
    if not _LIBS:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        xc = ctypes.CDLL(ctypes.util.find_library("Xcomposite") or "libXcomposite.so.1")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XGetImage.restype = ctypes.POINTER(_XImage)
        xc.XCompositeNameWindowPixmap.restype = ctypes.c_ulong
        x11.XSetErrorHandler(_HANDLER)
        _LIBS.update(x11=x11, xc=xc)
    return _LIBS["x11"], _LIBS["xc"]


class _Display:
    def __init__(self):
        self.x11, self.xc = _libs()
        self.dpy = self.x11.XOpenDisplay(None)
        if not self.dpy:
            raise XError("XOpenDisplay failed (DISPLAY?)")
        self.p = ctypes.c_void_p(self.dpy)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.x11.XCloseDisplay(self.p)

    def sync_errors(self) -> list[int]:
        self.x11.XSync(self.p, 0)
        errs = list(_ERRS)
        _ERRS.clear()
        return errs

    def children(self, w: int) -> tuple[int, int, list[int]]:
        root, parent, kids, n = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        if not self.x11.XQueryTree(self.p, ctypes.c_ulong(w), ctypes.byref(root), ctypes.byref(parent),
                                   ctypes.byref(kids), ctypes.byref(n)):
            return 0, 0, []
        out = [kids[i] for i in range(n.value)]
        if kids:
            self.x11.XFree(kids)
        return root.value, parent.value, out

    def attributes(self, w: int) -> _XWindowAttributes:
        att = _XWindowAttributes()
        self.x11.XGetWindowAttributes(self.p, ctypes.c_ulong(w), ctypes.byref(att))
        return att

    def cardinal(self, w: int, atom: int) -> int | None:
        typ, fmt, n, after = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong()
        prop = ctypes.POINTER(ctypes.c_ubyte)()
        rc = self.x11.XGetWindowProperty(self.p, ctypes.c_ulong(w), ctypes.c_ulong(atom), ctypes.c_long(0),
                                         ctypes.c_long(1), 0, ctypes.c_ulong(XA_CARDINAL), ctypes.byref(typ),
                                         ctypes.byref(fmt), ctypes.byref(n), ctypes.byref(after), ctypes.byref(prop))
        try:
            if rc != 0 or not prop or n.value < 1 or fmt.value != 32:
                return None
            return int(ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong))[0])   # format 32: an array of longs
        finally:
            if prop:
                self.x11.XFree(prop)


def windows_of_pid(pid: int) -> list[dict]:
    """The viewable windows (the frame's client, or the top-level itself) whose _NET_WM_PID is pid, largest first:
    {"client", "frame" (the top-level ancestor, the redirected window), "size" (client), "frame_size"}."""
    out = []
    with _Display() as d:
        atom = d.x11.XInternAtom(d.p, b"_NET_WM_PID", 1)
        if not atom:
            return []
        root = d.x11.XDefaultRootWindow(d.p)
        for top in d.children(root)[2]:
            for w in [top] + d.children(top)[2]:
                if d.cardinal(w, atom) != pid:
                    continue
                a, fa = d.attributes(w), d.attributes(top)
                if a.map_state == IS_VIEWABLE and fa.map_state == IS_VIEWABLE:
                    out.append({"client": hex(w), "frame": hex(top), "size": [a.width, a.height],
                                "frame_size": [fa.width, fa.height], "client_in_frame": [a.x, a.y] if w != top else [0, 0]})
                break
        d.sync_errors()
    return sorted(out, key=lambda x: -x["size"][0] * x["size"][1])


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [("type", _c.c_int), ("serial", _c.c_ulong), ("send_event", _c.c_int), ("display", _c.c_void_p),
                ("window", _c.c_ulong), ("message_type", _c.c_ulong), ("format", _c.c_int), ("l", _c.c_long * 5)]


class _XEvent(ctypes.Union):
    _fields_ = [("xclient", _XClientMessageEvent), ("pad", _c.c_long * 24)]


CLIENT_MESSAGE, SUBSTRUCTURE_REDIRECT_MASK, SUBSTRUCTURE_NOTIFY_MASK = 33, 1 << 20, 1 << 19


def window_at(x: int, y: int) -> str | None:
    """The top-level window (a child of the root) shown at root point (x, y): XTranslateCoordinates' child, the topmost
    mapped child containing the point; None when it names none (t53 fullscreen-in-trio: is the slot's window on top?)."""
    with _Display() as d:
        root = d.x11.XDefaultRootWindow(d.p)
        dx, dy, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        ok = d.x11.XTranslateCoordinates(d.p, ctypes.c_ulong(root), ctypes.c_ulong(root), int(x), int(y),
                                         ctypes.byref(dx), ctypes.byref(dy), ctypes.byref(child))
        d.sync_errors()
        return hex(child.value) if ok and child.value else None


def stacking() -> list[str]:
    """The root's viewable children, bottom to top (XQueryTree's order)."""
    with _Display() as d:
        root = d.x11.XDefaultRootWindow(d.p)
        out = [hex(w) for w in d.children(root)[2] if d.attributes(w).map_state == IS_VIEWABLE]
        d.sync_errors()
        return out


def activate(client: str | int) -> bool:
    """_NET_ACTIVE_WINDOW for `client` to the root (source indication 2, a pager: honoured past focus-stealing
    prevention) and XRaiseWindow; True when sent without an X error."""
    w = int(client, 16) if isinstance(client, str) else int(client)
    with _Display() as d:
        root = d.x11.XDefaultRootWindow(d.p)
        atom = d.x11.XInternAtom(d.p, b"_NET_ACTIVE_WINDOW", 0)
        ev = _XEvent()
        ev.xclient.type, ev.xclient.send_event, ev.xclient.display = CLIENT_MESSAGE, 1, d.dpy
        ev.xclient.window, ev.xclient.message_type, ev.xclient.format = w, atom, 32
        ev.xclient.l[0], ev.xclient.l[1], ev.xclient.l[2] = 2, 0, 0
        d.x11.XSendEvent(d.p, ctypes.c_ulong(root), 0, SUBSTRUCTURE_REDIRECT_MASK | SUBSTRUCTURE_NOTIFY_MASK,
                         ctypes.byref(ev))
        d.x11.XRaiseWindow(d.p, ctypes.c_ulong(w))
        return not d.sync_errors()


def grab(frame: str | int):
    """(PIL RGB image, record) of a top-level frame's own pixmap. XError when the frame is not redirected (BadMatch:
    e.g. an unredirected fullscreen window), gone, or the pixmap cannot be read."""
    from PIL import Image
    w = int(frame, 16) if isinstance(frame, str) else int(frame)
    with _Display() as d:
        ev, er = ctypes.c_int(), ctypes.c_int()
        if not d.xc.XCompositeQueryExtension(d.p, ctypes.byref(ev), ctypes.byref(er)):
            raise XError("no Composite extension")
        att = d.attributes(w)
        if d.sync_errors() or att.width <= 0:
            raise XError(f"window {hex(w)} gone")
        pix = d.xc.XCompositeNameWindowPixmap(d.p, ctypes.c_ulong(w))
        errs = d.sync_errors()
        if errs or not pix:
            raise XError(f"NameWindowPixmap {hex(w)}: X error {errs} (not redirected)")
        try:
            img = d.x11.XGetImage(d.p, ctypes.c_ulong(pix), 0, 0, att.width, att.height, ctypes.c_ulong(0xFFFFFFFF), Z_PIXMAP)
            errs = d.sync_errors()
            if errs or not img:
                raise XError(f"XGetImage on {hex(w)}'s pixmap: X error {errs}")
            im = img.contents
            try:
                if im.bits_per_pixel not in (24, 32):
                    raise XError(f"pixmap bits per pixel {im.bits_per_pixel}")
                buf = ctypes.string_at(im.data, im.bytes_per_line * im.height)
                raw = (("BGRX" if im.byte_order == 0 else "XRGB") if im.bits_per_pixel == 32
                       else ("BGR" if im.byte_order == 0 else "RGB"))
                out = Image.frombuffer("RGB", (im.width, im.height), buf, "raw", raw, im.bytes_per_line, 1).copy()
                rec = {"frame": hex(w), "size": [im.width, im.height], "depth": att.depth, "bpp": im.bits_per_pixel}
            finally:                                  # XDestroyImage is a macro: an XGetImage image is its data and itself
                d.x11.XFree(im.data)
                d.x11.XFree(img)
        finally:
            d.x11.XFreePixmap(d.p, ctypes.c_ulong(pix))
    return out, rec
