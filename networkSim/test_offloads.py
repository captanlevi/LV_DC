#!/usr/bin/env python3
"""offloads (t55, rushi 2026-10-19: GRO off from the first counted round): `ethtool -k` parsing (sub-features, [fixed]),
ensure_off's per-feature calls and read-back, a refused flag (fixed on) failing, an absent feature not blocking, and a
failed read failing. A fake ethtool.

    python3 test_offloads.py        (exit 0 = pass)
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import offloads as O  # noqa: E402

K = """Features for wlp0s20f3:
rx-checksumming: on
tx-checksumming: on
\ttx-checksum-ipv4: off [fixed]
tcp-segmentation-offload: {tso}
\ttx-tcp-segmentation: {tso}
\ttx-tcp6-segmentation: {tso}
generic-segmentation-offload: {gso}
generic-receive-offload: {gro}
large-receive-offload: off [fixed]
rx-gro-hw: off [fixed]
rx-gro-list: {grol}
{udpfwd}"""


class FakeEthtool:
    def __init__(self, gro="on", gso="on", tso="on", grol="off", udpfwd="on", refuse=(), fail_read=False):
        self.s = {"gro": gro, "gso": gso, "tso": tso, "grol": grol, "udpfwd": udpfwd}
        self.refuse, self.fail_read, self.calls = set(refuse), fail_read, []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(cmd)
        if cmd[:2] == ["ethtool", "-k"]:
            if self.fail_read:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="Cannot get device feature")
            u = "" if self.s["udpfwd"] is None else f"rx-udp-gro-forwarding: {self.s['udpfwd']}"
            return types.SimpleNamespace(returncode=0, stdout=K.format(udpfwd=u, **{k: v for k, v in self.s.items() if k != "udpfwd"}),
                                         stderr="")
        short = cmd[-2]
        key = {"gro": "gro", "gso": "gso", "tso": "tso", "rx-gro-list": "grol", "rx-udp-gro-forwarding": "udpfwd"}[short]
        if short in self.refuse:
            return types.SimpleNamespace(returncode=1, stdout="", stderr=f"Could not change any device features ({short})")
        self.s[key] = "off"
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def main() -> int:
    f = O.parse(K.format(gro="on", gso="on", tso="on", grol="off", udpfwd=""))
    assert f["generic-receive-offload"] == {"on": True, "fixed": False}
    assert f["large-receive-offload"] == {"on": False, "fixed": True} and f["tx-tcp6-segmentation"]["on"] is True
    assert "Features for wlp0s20f3" not in f
    eth = FakeEthtool()
    r = O.ensure_off("wlp0s20f3", run=eth)
    assert r["ok"] and all(v["on"] is False for v in r["after"].values()), r
    assert r["before"]["generic-receive-offload"]["on"] is True
    sets = [c for c in eth.calls if c[:3] == ["sudo", "-n", "ethtool"]]
    assert [c[-2] for c in sets] == list(O.OFF) and all(c[3:5] == ["-K", "wlp0s20f3"] and c[-1] == "off" for c in sets)
    # a flag the driver refuses (fixed on): not ok, the refusal recorded
    r = O.ensure_off("wlp0s20f3", run=FakeEthtool(refuse=("gro",)))
    assert r["ok"] is False and r["after"]["generic-receive-offload"]["on"] is True
    assert [c for c in r["calls"] if c["feature"] == "gro"][0]["rc"] == 1
    # a feature the kernel does not list: not called, cannot be on
    eth = FakeEthtool(udpfwd=None)
    r = O.ensure_off("wlp0s20f3", run=eth)
    assert r["ok"] and r["after"]["rx-udp-gro-forwarding"] == "absent" and "rx-udp-gro-forwarding" not in [c["feature"] for c in r["calls"]]
    # the read itself fails: not ok
    r = O.ensure_off("wlp0s20f3", run=FakeEthtool(fail_read=True))
    assert r["ok"] is False and "read_error" in r
    # P3 (levi 00120): a capture IFB gets IFB_OFF (segmentation incl. UDP GSO and gso-partial, coalescing), one call each
    class FakeIfb:
        def __init__(self):
            self.s = {"tcp-segmentation-offload": "on", "generic-segmentation-offload": "on", "tx-udp-segmentation": "on",
                      "tx-gso-partial": "on", "generic-receive-offload": "on", "rx-gro-list": "off"}
            self.calls = []

        def __call__(self, cmd, capture_output=True, text=True, timeout=None):
            self.calls.append(cmd)
            if cmd[:2] == ["ethtool", "-k"]:
                return types.SimpleNamespace(returncode=0, stderr="", stdout="Features for lvo_yt:\n" +
                                             "".join(f"{k}: {v}\n" for k, v in self.s.items()))
            self.s[O.IFB_OFF[cmd[-2]]] = "off"
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ifb = FakeIfb()
    r = O.ensure_off("lvo_yt", run=ifb, features=O.IFB_OFF)
    assert r["ok"] and r["requested_off"] == list(O.IFB_OFF) and r["after"]["tx-udp-segmentation"]["on"] is False, r
    assert sorted(c[-2] for c in ifb.calls if c[0] == "sudo") == sorted(O.IFB_OFF)
    assert O.read("lvo_yt", run=FakeIfb(), features=O.IFB_OFF)["all_off"] is False
    assert O.read("wlp0s20f3", run=FakeEthtool(gro="off", gso="off", tso="off", udpfwd="off"))["all_off"] is True
    print("test_offloads: ok")
    return 0


def test_offloads():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
