"""t55 (rushi 2026-10-19: GRO off on chaos from the first counted round): the capture interface's coalescing and
segmentation offloads are switched off at collector start and the state is recorded on every session, both modes.

GRO (and its UDP / list variants) coalesces received packets in the driver's NAPI poll, before tc ingress and so before
the downstream capture on the IFB; GSO / TSO hand pre-segmentation buffers to tc egress, where the upstream capture
mirror sits. Off, each captured frame is one packet on the wire. The setting holds for the era: nothing restores it.

  ensure_off(iface)  set every OFF feature off (sudo -n ethtool -K, one feature per call), read `ethtool -k` back; the
                     record says what was requested, what each call returned and the state read after; ok only when
                     every OFF feature the kernel lists reads off (a feature the kernel does not list cannot be on)
  read(iface)        the current state of the OFF features (no change)
"""
from __future__ import annotations

import subprocess

IFACE_DEFAULT = "wlp0s20f3"
# ethtool -K short name -> the `ethtool -k` feature name read back
OFF = {"gro": "generic-receive-offload", "gso": "generic-segmentation-offload", "tso": "tcp-segmentation-offload",
       "rx-gro-list": "rx-gro-list", "rx-udp-gro-forwarding": "rx-udp-gro-forwarding"}


def parse(text: str) -> dict:
    """`ethtool -k` text -> {feature: {"on": bool, "fixed": bool}} (sub-features, indented, included)."""
    out = {}
    for line in (text or "").splitlines():
        name, sep, rest = line.strip().partition(":")
        if not sep or not name or name.startswith("Features for"):
            continue
        val = rest.strip()
        if not val:
            continue
        out[name] = {"on": val.split()[0] == "on", "fixed": "[fixed]" in val}
    return out


def _run(cmd: list[str], run=subprocess.run) -> tuple[int, str, str]:
    try:
        r = run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout, (r.stderr or "").strip()[:300]
    except Exception as e:
        return -1, "", repr(e)[:300]


def read(iface: str = IFACE_DEFAULT, run=subprocess.run) -> dict:
    """The OFF features' state on iface: {feature: {on, fixed} | "absent"}, all_off, and the raw read's rc / error."""
    rc, out, err = _run(["ethtool", "-k", iface], run)
    feats = parse(out) if rc == 0 else {}
    state = {name: feats.get(name, "absent") for name in OFF.values()}
    return {"iface": iface, "rc": rc, **({"error": err} if rc else {}), "features": state,
            "all_off": rc == 0 and all(v == "absent" or not v["on"] for v in state.values())}


def ensure_off(iface: str = IFACE_DEFAULT, run=subprocess.run) -> dict:
    """Set the OFF features off (one call each, so one refusal does not hide the others) and read back. ok: every
    listed OFF feature reads off. Never raises; the caller fails loudly on ok false."""
    before = read(iface, run)
    calls = []
    for short, name in OFF.items():
        if before["features"].get(name) == "absent":
            continue
        rc, _, err = _run(["sudo", "-n", "ethtool", "-K", iface, short, "off"], run)
        calls.append({"feature": short, "rc": rc, **({"error": err} if rc else {})})
    after = read(iface, run)
    return {"iface": iface, "requested_off": list(OFF), "before": before["features"], "calls": calls,
            "after": after["features"], "ok": after["all_off"], **({"read_error": after["error"]} if after.get("error") else {})}
