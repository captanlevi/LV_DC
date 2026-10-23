#!/usr/bin/env python3
"""W4 (2) / (3) readers on synthetic captures with known answers (levi a9d73b69; laptop, needs tshark):
 - ops/diag/rtt_delay.py: TCP SYN / SYN-ACK and QUIC Initial pairs built with dpkt at RTT = path + 2·D, under a
   net_labels delay change: every handshake passes C6, one planted bypass (RTT < 2D - 5 ms) fails it, the handshake
   inside the change guard is dropped, and the within-/24 slope reads 1;
 - ops/diag/loss_drops.py: TCP segments first captured as retransmissions are counted missing (a re-sent captured
   segment is not); the C6 verdict is one-sided by default, two-sided for keyed-QUIC-only sessions, not applicable
   without zero kernel capture drops; the PN-skip baseline is subtracted.

    python3 test_w4_rtt_loss.py        (exit 0 = pass)
"""
import importlib.util
import json
import shutil
import socket
import struct
import sys
import tempfile
from pathlib import Path

import dpkt

HERE = Path(__file__).resolve().parent
DIAG = HERE.parent / "ops" / "diag"
ME, T0 = "192.168.1.10", 1.79e9


def _mod(name: str):
    spec = importlib.util.spec_from_file_location(name, DIAG / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RD, LD = _mod("rtt_delay"), _mod("loss_drops")


def _eth(ip):
    return bytes(dpkt.ethernet.Ethernet(src=b"\x00" * 6, dst=b"\x00" * 6, type=0x0800, data=ip))


def _ip(src, dst, p, l4):
    ip = dpkt.ip.IP(src=socket.inet_aton(src), dst=socket.inet_aton(dst), p=p, data=l4)
    ip.len = len(ip)
    return ip


def _tcp(src, dst, sp, dp, flags, seq=1000, data=b""):
    return _eth(_ip(src, dst, 6, dpkt.tcp.TCP(sport=sp, dport=dp, flags=flags, seq=seq, off=5, data=data)))


def _quic(src, dst, sp, dp, ptype=0):
    body = bytes([0xC0 | (ptype << 4)]) + struct.pack("!I", 1) + b"\x08" + b"\x01" * 8 + b"\x08" + b"\x02" * 8
    body += (b"\x00" if ptype == 0 else b"") + b"\x40\x20" + b"\x00" * 32
    u = dpkt.udp.UDP(sport=sp, dport=dp, data=body)
    u.ulen = len(u)
    return _eth(_ip(src, dst, 17, u))


def _write(path: Path, pkts):
    with open(path, "wb") as f:
        w = dpkt.pcap.Writer(f, linktype=1)
        for t, b in sorted(pkts, key=lambda x: x[0]):
            w.writepkt(b, ts=t)


def test_rtt_delay():
    tshark = shutil.which("tshark")
    assert tshark, "tshark not found: the W4 RTT reader cannot be tested"
    with tempfile.TemporaryDirectory() as t:
        sd = Path(t) / "s"
        sd.mkdir()
        (sd / "meta.json").write_text(json.dumps({"platform": "youtube"}))
        # net_labels as on the record: a row per shaper apply, every 2 s; delay_ms changes at the T0 + 30 row
        (sd / "net_labels.txt").write_text("".join(json.dumps({"timestamp": T0 + 2.0 * k, "delay_ms": 40 if 2.0 * k < 30 else 100,
                                                               "speed": 1000}) + "\n" for k in range(30)))
        pk, port = [], 40000
        path_ms = {"1.2.3.4": 20.0, "1.2.3.5": 22.0, "5.6.7.8": 35.0}
        for i, t0 in enumerate([T0 + 5, T0 + 10, T0 + 20, T0 + 35, T0 + 40, T0 + 50]):
            d = 40 if t0 < T0 + 30 else 100
            for srv, base in path_ms.items():
                port += 1
                rtt = (base + 2 * d) / 1000
                if i % 2:
                    pk += [(t0, _tcp(ME, srv, port, 443, dpkt.tcp.TH_SYN)), (t0 + rtt, _tcp(srv, ME, 443, port, dpkt.tcp.TH_SYN | dpkt.tcp.TH_ACK))]
                else:
                    pk += [(t0, _quic(ME, srv, port, 443)), (t0 + rtt, _quic(srv, ME, 443, port))]
        pk += [(T0 + 25, _tcp(ME, "9.9.9.9", 50001, 443, dpkt.tcp.TH_SYN)),            # a bypass: RTT 30 ms under D = 40
               (T0 + 25.03, _tcp("9.9.9.9", ME, 443, 50001, dpkt.tcp.TH_SYN | dpkt.tcp.TH_ACK)),
               (T0 + 29.6, _tcp(ME, "1.2.3.4", 50002, 443, dpkt.tcp.TH_SYN)),          # straddles the change: dropped
               (T0 + 29.7, _tcp("1.2.3.4", ME, 443, 50002, dpkt.tcp.TH_SYN | dpkt.tcp.TH_ACK))]
        _write(sd / "merged.pcap", pk)
        r = RD.session(sd, tshark)
        assert r["client"] == ME and r["dropped_near_change"] == 1, r
        assert r["handshakes"] == 19 and r["n_c6_violations"] == 1 and r["c6_violations"][0]["server"] == "9.9.9.9", r
        assert {x["proto"] for x in r["rows"]} == {"tcp", "quic"}
        s = RD.within_slope([x for x in r["rows"] if x["c6_ok"]])
        assert s["in_band"] and abs(s["slope"] - 1) < 0.01 and s["groups"] == 2, s          # 1.2.3.4 and 1.2.3.5 share a /24
        # the change at T0 + 30 between two rows (T0 + 29, T0 + 31): the straddling handshake at T0 + 29.6 is > 1 s from
        # the change row and is still dropped, because the change lies anywhere in (previous row, change row]
        (sd / "net_labels.txt").write_text("".join(json.dumps({"timestamp": T0 + 1 + 2.0 * k, "delay_ms": 40 if 1 + 2.0 * k < 30
                                                               else 100, "speed": 1000}) + "\n" for k in range(30)))
        r3 = RD.session(sd, tshark)
        assert r3["dropped_near_change"] == 1 and r3["handshakes"] == 19 and r3["n_c6_violations"] == 1, r3


def test_loss_drops():
    rows = [["0", "1.2.3.4", "1000", "100"], ["0", "1.2.3.4", "1100", "100"],
            ["0", "1.2.3.4", "1300", "100"],                     # 1200 never captured (dropped before the tap)
            ["0", "1.2.3.4", "1200", "100"],                     # its retransmission: missing 1
            ["0", "1.2.3.4", "1100", "100"],                     # a re-send of a captured segment: not missing
            ["0", ME, "5000", "50"]]                             # upstream: ignored
    assert LD.tcp_missing(rows, ME) == (5, 1)
    assert LD.verdict(10, 10, False, 0)["c6"] == "pass"
    assert LD.verdict(30, 10, False, 0)["c6"] == "fail"                     # more missing than the class dropped
    assert LD.verdict(0, 100, False, 0)["c6"] == "pass"                     # one-sided: fewer missing is allowed
    assert LD.verdict(0, 100, True, 0)["c6"] == "fail"                      # two-sided for keyed QUIC only
    assert LD.verdict(98, 100, True, 0)["c6"] == "pass"
    assert LD.verdict(10, 10, False, None)["c6"] == "not_applicable"        # kernel drops unknown
    assert LD.verdict(10, 10, False, 3)["c6"] == "not_applicable"
    assert LD.verdict(10, None, False, 0)["c6"] == "not_applicable"
    with tempfile.TemporaryDirectory() as t:
        sd = Path(t) / "s"
        sd.mkdir()
        (sd / "merged.pcap").write_bytes(b"")
        (sd / "meta.json").write_text(json.dumps({"platform": "youtube", "concurrency": {"capture_drops": {"dropped_kernel_total": 0}}}))
        (sd / "tc_stats.jsonl").write_text("".join(json.dumps({"ts": T0 + i, "in": {"drops": d}}) + "\n" for i, d in enumerate((0, 40, 100))))
        saved = (LD.quic_down, LD.tcp_rows)
        LD.quic_down = lambda sd, ts: {"client": ME, "connections": 2, "keyed": 2, "unkeyed": 0, "packets": 10000, "gaps": 120}
        LD.tcp_rows = lambda sd, ts: []
        try:
            r = LD.session(sd, "tshark", skip_rate=0.002)          # 120 gaps - 0.002 x 10000 skips = 100 missing
        finally:
            LD.quic_down, LD.tcp_rows = saved
        assert r["drops"] == 100 and r["missing"] == 100.0 and r["keyed_quic_only"] and r["c6"] == "pass", r


def main() -> int:
    test_rtt_delay()
    test_loss_drops()
    print("test_w4_rtt_loss: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
