#!/usr/bin/env python3
"""Decision A item 4 (levi 00271): session_meta.display_state reads the DPMS state (`xset q`) and the display topology
(`xrandr --listproviders`, the connected DRM connectors' drivers), read only; a missing tool leaves its fields None."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_meta as M  # noqa: E402

XSET = """DPMS (Energy Star):
  Standby: 600    Suspend: 600    Off: 600
  DPMS is Enabled
  Monitor is Off
"""
PROV = """Providers: number : 2
Provider 0: id: 0x43 cap: 0x9, Source Output, Sink Offload crtcs: 4 outputs: 1 associated providers: 1 name:modesetting
Provider 1: id: 0x1fe cap: 0x2, Sink Output crtcs: 4 outputs: 5 associated providers: 1 name:NVIDIA-G0
"""


def _drm(root: Path) -> None:
    """chaos's layout: card1 i915 (eDP, disconnected), card2 nvidia (HDMI connected)."""
    for card, drv in (("card1", "i915"), ("card2", "nvidia")):
        (root / "drivers" / drv).mkdir(parents=True)
        (root / card / "device").mkdir(parents=True)
        os.symlink(root / "drivers" / drv, root / card / "device" / "driver")
    for conn, st in (("card1-eDP-1", "disconnected"), ("card2-HDMI-A-1", "connected"), ("card2-DP-1", "disconnected")):
        (root / conn).mkdir()
        (root / conn / "status").write_text(st + "\n")


def test_display_state():
    saved = M._run
    try:
        with tempfile.TemporaryDirectory() as d:
            _drm(Path(d))
            M._run = lambda cmd, timeout=10.0: {"xset": XSET, "xrandr": PROV}[cmd[0]]
            s = M.display_state(d)
            assert s["dpms_enabled"] is True and s["monitor"] == "Off", s
            assert len(s["providers"]) == 2 and s["providers"][1].endswith("name:NVIDIA-G0"), s
            assert s["connected"] == [{"connector": "card2-HDMI-A-1", "driver": "nvidia"}], s
            M._run = lambda cmd, timeout=10.0: XSET.replace("Enabled", "Disabled").replace("Off\n", "On\n") \
                if cmd[0] == "xset" else None
            s = M.display_state(d)
            assert s["dpms_enabled"] is False and s["monitor"] == "On" and s["providers"] is None, s
            M._run = lambda cmd, timeout=10.0: None                  # no X tools (the laptop): fields None, never raises
            s = M.display_state(str(Path(d) / "absent"))
            assert s["dpms_enabled"] is None and s["monitor"] is None and s["connected"] == [], s
    finally:
        M._run = saved


def test_display_change():
    """Hardening stop: a forced-off start and an On end is a wake; Off -> Off, On -> On and an unreadable side are not;
    exp.py records it beside display_end."""
    off, on = {"monitor": "Off", "dpms_enabled": True}, {"monitor": "On", "dpms_enabled": True}
    assert M.display_change(off, on) == {"monitor": ["Off", "On"], "woke": True, "dpms_enabled": [True, True]}
    assert not M.display_change(off, off)["woke"] and not M.display_change(on, on)["woke"]
    assert not M.display_change({"monitor": None}, on)["woke"] and not M.display_change(None, None)["woke"]
    assert M.display_change({"monitor": "in Standby"}, on)["woke"]
    src = (Path(__file__).resolve().parent / "exp.py").read_text()
    assert 'meta_extra["display_change"] = session_meta.display_change(_display_start, meta_extra["display_end"])' in src


if __name__ == "__main__":
    test_display_state()
    test_display_change()
    print("test_display_state: ok")
