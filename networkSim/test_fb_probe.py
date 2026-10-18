#!/usr/bin/env python3
"""ops/diag/fb_layout_check.py v3 (t53 pilot-1 layout probe): the identity rule (a grab is a window's own pixels only
when its own capture is accepted and the other instance's is not; v2's false positive: the occluded window's grab was
the top window's pixels), the verdicts (B void without the root-grab discriminator), the per-instance pages (distinct
seeds, the second inverted) and the ctypes X structures' sizes (LP64).

    python3 test_fb_probe.py        (exit 0 = pass)
"""
import ctypes
import sys
import types
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "ops" / "diag"))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.SimpleNamespace(WebSocket=object, WebSocketTimeoutException=Exception)
import fb_layout_check as F  # noqa: E402

OK = {"accepted": True, "residual": 0.3}
NO = {"accepted": False, "residual": 99.0}


def _grab(own_ok=True, other_ok=False, live=True):
    return {"identity": F.identity(OK if own_ok else NO, OK if other_ok else NO), "live": {"live": live}}


def test_identity():
    assert F.identity(OK, NO)["ok"] is True
    assert F.identity(OK, OK)["ok"] is False          # v2: both pages matched (one pattern), so the grab says nothing
    assert F.identity(NO, OK)["ok"] is False          # the other window's pixels (v2's under grab)
    assert F.identity(NO, NO)["ok"] is False


def test_verdicts():
    p0, p1 = (str(p) for p in F.PORTS)
    good = {"A": {"placed_off_monitor": True, "root_grab": _grab(), "state": {"raf_fps": 60}},
            "B": {"discriminator_ok": True,
                  p0: {"role": "under", "state": {"raf_fps": 60}, "grabs": {"composite": _grab(), "xwd": _grab(own_ok=False, other_ok=True)}},
                  p1: {"role": "over", "state": {"raf_fps": 60}, "grabs": {"composite": _grab(), "xwd": _grab()}}}}
    v = F.verdicts(good)
    assert v == {"A_virtual_monitor": True, "discriminator_ok": True, "B_composite": True, "B_xwd": False}, v
    void = {**good, "B": {**good["B"], "discriminator_ok": False}}
    assert F.verdicts(void)["B_composite"] is None and F.verdicts(void)["B_xwd"] is None
    clamped = {**good, "A": {**good["A"], "placed_off_monitor": False}}
    assert F.verdicts(clamped)["A_virtual_monitor"] is False
    slow = {**good, "B": {**good["B"], p0: {**good["B"][p0], "state": {"raf_fps": 1}}}}
    assert F.verdicts(slow)["B_composite"] is False   # the occluded window throttled
    stale = {**good, "B": {**good["B"], p1: {**good["B"][p1], "grabs": {"composite": _grab(live=False)}}}}
    assert F.verdicts(stale)["B_composite"] is False
    assert F.verdicts({})["A_virtual_monitor"] is False and F.verdicts({})["B_composite"] is None


def test_pages_differ_per_instance():
    a, b = (urllib.parse.unquote(F.page_url(p)) for p in F.PORTS)
    assert "fbprobe-9333" in a and "fbprobe-9334" in b
    assert "inv=0" in a and "inv=1" in b
    seeds = [p * 2654435761 % 2**32 for p in F.PORTS]
    assert seeds[0] != seeds[1] and f"s={seeds[0]}>>>0" in a and f"s={seeds[1]}>>>0" in b


def test_x_struct_sizes():
    if ctypes.sizeof(ctypes.c_long) != 8:
        return                                        # the probe runs on LP64 Linux (chaos)
    assert ctypes.sizeof(F._XWindowAttributes) == 136
    assert F._XErrorEvent.error_code.offset == 32
    assert F._XImage.bits_per_pixel.offset == 48 and F._XImage.data.offset == 16


def main() -> int:
    test_identity()
    test_verdicts()
    test_pages_differ_per_instance()
    test_x_struct_sizes()
    print("test_fb_probe: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
