"""t55 (rushi 2026-10-19: GRO off on chaos from the first counted round): the capture interface's coalescing and
segmentation offloads are switched off at collector start and the state is recorded on every session, both modes.

GRO (and its UDP / list variants) coalesces received packets in the driver's NAPI poll, before tc ingress and so before
the downstream capture on the IFB; GSO / TSO hand pre-segmentation buffers to tc egress, where the upstream capture
mirror sits. Off, each captured frame is one packet on the wire. The setting holds for the era: nothing restores it.

  ensure_off(iface)  set every OFF feature off (sudo -n ethtool -K, one feature per call), read `ethtool -k` back; the
                     record says what was requested, what each call returned and the state read after; ok only when
                     every OFF feature the kernel lists reads off (a feature the kernel does not list cannot be on)
  read(iface)        the current state of the OFF features (no change)

Capture IFBs (P3 on counted round 1, levi 00120; peter's ops test): Chrome's TCP (and QUIC's UDP GSO) hand super-skbs to
tc egress even with the interface's gso / tso off; the egress mirror clones them whole onto the capture IFB, which
segments at its own dequeue only when its own features are off, so its tap saw frames above the MSS upstream. Every
capture IFB gets IFB_OFF off (ensure_off(dev, features=IFB_OFF)), recorded per device; HTB still releases a super-skb
upstream as one unit (its pieces leave back to back).
"""
from __future__ import annotations

import subprocess

IFACE_DEFAULT = "wlp0s20f3"
# ethtool -K short name -> the `ethtool -k` feature name read back
OFF = {"gro": "generic-receive-offload", "gso": "generic-segmentation-offload", "tso": "tcp-segmentation-offload",
       "rx-gro-list": "rx-gro-list", "rx-udp-gro-forwarding": "rx-udp-gro-forwarding"}
IFB_OFF = {"tso": "tcp-segmentation-offload", "gso": "generic-segmentation-offload",
           "tx-udp-segmentation": "tx-udp-segmentation", "tx-gso-partial": "tx-gso-partial",
           "gro": "generic-receive-offload", "rx-gro-list": "rx-gro-list"}


# hardening stop (coverage audit): these three are on every NIC and IFB `ethtool -k` lists; absent = an unreadable or
# reformatted read, never "off" (the optional features may be absent on an older kernel)
REQUIRED = ("generic-receive-offload", "generic-segmentation-offload", "tcp-segmentation-offload")


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


def read(iface: str = IFACE_DEFAULT, run=subprocess.run, features: dict = OFF) -> dict:
    """The features' state on iface (OFF by default; IFB_OFF for a capture IFB): {feature: {on, fixed} | "absent"},
    all_off, and the raw read's rc / error."""
    rc, out, err = _run(["ethtool", "-k", iface], run)
    feats = parse(out) if rc == 0 else {}
    state = {name: feats.get(name, "absent") for name in features.values()}
    return {"iface": iface, "rc": rc, **({"error": err} if rc else {}), "features": state,
            "all_off": rc == 0 and all(v == "absent" or not v["on"] for v in state.values())
            and not any(state.get(n) == "absent" for n in REQUIRED)}


def ensure_off(iface: str = IFACE_DEFAULT, run=subprocess.run, features: dict = OFF) -> dict:
    """Set the features off (OFF by default; IFB_OFF for a capture IFB; one call each, so one refusal does not hide
    the others) and read back. ok: every listed feature reads off. Never raises; the caller fails loudly on ok false."""
    before = read(iface, run, features)
    calls = []
    for short, name in features.items():
        if before["features"].get(name) == "absent":
            continue
        rc, _, err = _run(["sudo", "-n", "ethtool", "-K", iface, short, "off"], run)
        calls.append({"feature": short, "rc": rc, **({"error": err} if rc else {})})
    after = read(iface, run, features)
    return {"iface": iface, "requested_off": list(features), "before": before["features"], "calls": calls,
            "after": after["features"], "ok": after["all_off"], **({"read_error": after["error"]} if after.get("error") else {})}
