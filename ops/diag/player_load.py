#!/usr/bin/env python3
"""A player-shaped load for the trio dry run (Q am. 16, master 62d949a9; peter's spec read by levi 2026-10-20 with four
changes). A collector-correctness tool: its fidelity read is an internal sanity figure, not a paper item.

Run inside a slot's cgroup (trio_dry_run's in_cgroup), so its packets carry the slot's mark like Chrome's, it fetches
segments from a public CDN size endpoint (default https://h3.speed.cloudflare.com/__down?bytes=N: any size, h2 / h3, a
WAN RTT like the real CDNs'; a box-local server would never cross the shaped interface; speed.cloudflare.com itself
serves no HTTP/3: its QUIC handshake ends in TLS alert 40, the 5ba0b94 dry run's 0 h3 segments) as a live-edge player:

  segment k becomes available at t0 + (k - J) x D, J = ceil(B / D) (the join: the player starts B behind the live edge
  and catches up back to back, as a real player does at join and after a stall); the player fetches the next segment
  once it is available and its buffer is below B, back to back while behind; playback drains the buffer in real time
  from the first segment on; an empty buffer before the next segment arrives is a stall (counted).
  D / B per platform (the measured cadence, spec §1-2): twitch 2 / 3 s, youtube 5 / 13 s, tiktok 1 s chunks with a new
  connection every 20 s and B 3 s (an assumption: tt records no buffer). A segment is the platform's highest ladder rung
  <= 0.8 x the state's set rate (else its lowest), times D (LV_DC coverage_report.RUNGS, kbit/s); no clamp.

Protocol per run: h3 (aioquic, PyPI, the diag venv; never a fallback to TCP: a failed h3 segment is an error) or tcp
(one keep-alive HTTPS connection; on repeated endpoint failure, the --fallback-url's HTTP Range fetch, TCP only, recorded
per segment). One JSON line per segment: t0, t1, bytes, ok, error, buffer_s, behind (segments behind the edge), proto,
alpn (negotiated), five_tuple (transport, local ip:port, remote ip:port), fallback; a summary line at the end. aioquic's
QUIC is not Chrome's (congestion control, pacing): a QUIC reading is of the shaper and the class only.

    python player_load.py --platform twitch --rate-kbit 1600 --proto h3 --seconds 59 --log player.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from urllib.parse import urlsplit

DEFAULT_URL = "https://h3.speed.cloudflare.com/__down"
# a QUIC CONNECTION_CLOSE in the crypto range (0x100 + the TLS alert): the alert's name, for the per-attempt cause
TLS_ALERTS = {10: "unexpected_message", 40: "handshake_failure", 42: "bad_certificate", 45: "certificate_expired",
              46: "certificate_unknown", 48: "unknown_ca", 50: "decode_error", 51: "decrypt_error",
              70: "protocol_version", 80: "internal_error", 109: "missing_extension", 112: "unrecognized_name",
              120: "no_application_protocol"}


class H3Error(Exception):
    """A failed h3 attempt with its cause (handshake close code / TLS alert, timeout, stream reset)."""


def close_cause(code: int | None, reason: str | None = None, frame_type: int | None = None) -> str:
    if code is None:
        return "no CONNECTION_CLOSE seen"
    out = f"quic close 0x{code:x}"
    if 0x100 <= code <= 0x1ff:
        a = code - 0x100
        out += f" (TLS alert {a} {TLS_ALERTS.get(a, 'unknown')})"
    if frame_type:
        out += f" frame 0x{frame_type:x}"
    return out + (f" reason {reason!r}" if reason else "")


def norm_addr(ip: str, port) -> str:
    """ip:port with an IPv4-mapped IPv6 address as plain IPv4 (aioquic's dual-stack socket reports ::ffff:a.b.c.d,
    tcpdump prints a.b.c.d: the leak check matches on the plain form)."""
    ip = str(ip)
    return f"{ip[7:] if ip.lower().startswith('::ffff:') else ip}:{port}"
FALLBACK_URL = "http://mirror.aarnet.edu.au/pub/ubuntu/releases/24.04/ubuntu-24.04.5-live-server-amd64.iso"
RUNG_SHARE = 0.8
# spec §2 (levi's read): segment cadence D and buffer target B per platform; tt reconnects every TT_RECONNECT_S
CADENCE = {"twitch": {"D": 2.0, "B": 3.0}, "youtube": {"D": 5.0, "B": 13.0}, "tiktok": {"D": 1.0, "B": 3.0}}
TT_RECONNECT_S = 20.0
# LV_DC coverage_report.RUNGS (kbit/s), the ladders the segment size reads (duplicated here so the dry run needs no
# collector import; test_player_load pins equality with coverage_report)
LADDERS = {"twitch": (230.0, 630.0, 1428.0, 3423.0, 7065.9),
           "youtube": (255.0, 265.0, 380.8, 672.0, 876.8, 3040.0),
           "tiktok": (600.0, 1000.0, 1800.0, 3000.0)}
FALLBACK_AFTER_ERRORS = 3


def rung_kbit(platform: str, rate_kbit: float) -> float:
    """The platform's highest ladder rung <= RUNG_SHARE x the set rate, else its lowest."""
    lad = sorted(LADDERS[platform])
    fit = [r for r in lad if r <= RUNG_SHARE * rate_kbit]
    return fit[-1] if fit else lad[0]


def segment_bytes(platform: str, rate_kbit: float) -> int:
    return int(rung_kbit(platform, rate_kbit) * 1000 / 8 * CADENCE[platform]["D"])


class Player:
    """The live-edge model over fetch(n_bytes) -> (bytes, info); clock / sleep injectable (tests)."""

    def __init__(self, fetch, seg_bytes: int, D: float, B: float, clock=time.monotonic, sleep=time.sleep, log=None,
                 reconnect=None, reconnect_s: float | None = None):
        self.fetch, self.seg_bytes, self.D, self.B = fetch, seg_bytes, D, B
        self.clock, self.sleep, self.log = clock, sleep, log
        self.reconnect, self.reconnect_s = reconnect, reconnect_s
        self.segments = self.bytes = self.stalls = self.errors = 0
        self.stall_s = self.fetch_s = 0.0

    def run(self, seconds: float) -> dict:
        t0 = self.clock()
        t_end = t0 + seconds
        J = math.ceil(self.B / self.D)                       # the join: B behind the live edge
        k, buf, playing, last = 0, 0.0, False, t0
        last_conn = t0

        def available(now):                                  # segments available by `now` (indexes < this)
            return J + int((now - t0) // self.D) + 1

        while self.clock() < t_end:
            now = self.clock()
            if playing:
                drained = now - last
                if drained > buf:                            # the buffer ran dry: a stall
                    self.stalls += 1
                    self.stall_s += drained - buf
                    buf = 0.0
                else:
                    buf -= drained
            last = now
            if self.reconnect and self.reconnect_s and now - last_conn >= self.reconnect_s:
                self.reconnect()                             # tt: a new connection every TT_RECONNECT_S
                last_conn = now
            if k < available(now) and buf < self.B:
                f0 = self.clock()
                try:
                    n, info, err = *self.fetch(self.seg_bytes), None
                except Exception as e:  # noqa: BLE001 -- a failed fetch is logged and counted, never fatal
                    n, info, err = 0, {}, repr(e)[:200]
                f1 = self.clock()
                if playing:
                    drained = f1 - last
                    if drained > buf:
                        self.stalls += 1
                        self.stall_s += drained - buf
                        buf = 0.0
                    else:
                        buf -= drained
                last = f1
                if n:
                    buf += self.D
                    k += 1
                    playing = True
                    self.segments += 1
                    self.bytes += n
                    self.fetch_s += f1 - f0
                else:
                    self.errors += 1
                if self.log:
                    self.log({"t0": round(f0, 3), "t1": round(f1, 3), "bytes": n, "ok": err is None, "error": err,
                              "buffer_s": round(buf, 2), "behind": max(0, available(f1) - k), **info})
                if err:
                    self.sleep(1.0)
            else:                                            # wait until the next segment exists and the buffer is below B
                t_avail = max(0.0, t0 + (k - J) * self.D - now) if k >= available(now) else 0.0
                t_buf = (buf - self.B + 0.01) if buf >= self.B else 0.0
                wait = max(0.01, t_avail, t_buf)
                self.sleep(min(wait, max(0.0, t_end - self.clock())))
        return {"segments": self.segments, "bytes": self.bytes, "errors": self.errors, "stalls": self.stalls,
                "stall_s": round(self.stall_s, 2), "fetch_s_mean": round(self.fetch_s / self.segments, 3) if self.segments else None,
                "seg_bytes": self.seg_bytes, "D": self.D, "B": self.B, "join_segments": math.ceil(self.B / self.D)}


def _tuple(sock, transport: str) -> dict | None:
    try:
        lo, re_ = sock.getsockname(), sock.getpeername()
        return {"transport": transport, "local": norm_addr(lo[0], lo[1]), "remote": norm_addr(re_[0], re_[1])}
    except Exception:  # noqa: BLE001
        return None


def tcp_fetcher(url: str, fallback_url: str | None = FALLBACK_URL):
    """One keep-alive HTTPS connection; GET <url>?bytes=N; after FALLBACK_AFTER_ERRORS consecutive failures, the
    fallback's HTTP Range fetch (TCP only), recorded per segment."""
    import http.client
    st = {"conn": None, "fails": 0, "fallback": False}

    def connect(u):
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        c = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=30)
        c.connect()
        return c

    def fetch(n: int):
        u = urlsplit(fallback_url if st["fallback"] else url)
        if st["conn"] is None:
            st["conn"] = connect(u)
        c = st["conn"]
        try:
            if st["fallback"]:
                c.request("GET", u.path, headers={"Range": f"bytes=0-{n - 1}", "User-Agent": "lvdc-player-load"})
            else:
                c.request("GET", f"{u.path}?bytes={n}", headers={"User-Agent": "lvdc-player-load"})
            r = c.getresponse()
            got = 0
            while True:
                b = r.read(65536)
                if not b:
                    break
                got += len(b)
            alpn = None
            try:
                alpn = c.sock.selected_alpn_protocol() if hasattr(c.sock, "selected_alpn_protocol") else None
            except Exception:  # noqa: BLE001
                pass
            st["fails"] = 0
            return got, {"proto": "tcp", "alpn": alpn or "http/1.1", "five_tuple": _tuple(c.sock, "tcp"),
                         "fallback": st["fallback"]}
        except Exception:
            c.close()
            st["conn"] = None
            st["fails"] += 1
            if fallback_url and not st["fallback"] and st["fails"] >= FALLBACK_AFTER_ERRORS:
                st["fallback"], st["fails"] = True, 0
            raise

    def reconnect():
        if st["conn"] is not None:
            st["conn"].close()
            st["conn"] = None
    return fetch, reconnect, reconnect


def h3_fetcher(url: str):
    """One QUIC connection (aioquic, HTTP/3), a new request stream per segment; never a TCP fallback."""
    import asyncio
    from aioquic.asyncio.client import connect
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import DataReceived, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import ConnectionTerminated, StreamReset

    u = urlsplit(url)
    st: dict = {"cm": None, "client": None, "proto": None}

    class H3Client(QuicConnectionProtocol):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.http = H3Connection(self._quic)
            self.waiters: dict = {}
            self.closed = None
            st["proto"] = self                   # the handshake's protocol, for its close cause if connect fails

        async def get(self, path: str) -> int:
            sid = self._quic.get_next_available_stream_id()
            self.http.send_headers(stream_id=sid, headers=[
                (b":method", b"GET"), (b":scheme", b"https"), (b":authority", u.hostname.encode()),
                (b":path", path.encode()), (b"user-agent", b"lvdc-player-load")], end_stream=True)
            fut = self._loop.create_future()
            self.waiters[sid] = [fut, 0]
            self.transmit()
            try:
                return await asyncio.wait_for(fut, timeout=30)
            except asyncio.TimeoutError:
                got = (self.waiters.pop(sid, None) or [None, 0])[1]
                raise H3Error(f"timeout: stream {sid} not ended in 30 s ({got} bytes; "
                              f"{close_cause(*self.closed) if self.closed else 'connection open'})") from None

        def quic_event_received(self, event):
            if isinstance(event, ConnectionTerminated):
                self.closed = (event.error_code, event.reason_phrase, event.frame_type)
                for w in self.waiters.values():
                    if not w[0].done():
                        w[0].set_exception(H3Error("connection closed: " + close_cause(*self.closed)))
                self.waiters.clear()
            if isinstance(event, StreamReset):
                w = self.waiters.pop(event.stream_id, None)
                if w is not None and not w[0].done():
                    w[0].set_exception(H3Error(f"stream {event.stream_id} reset, code 0x{event.error_code:x}"))
            for ev in self.http.handle_event(event):
                w = self.waiters.get(getattr(ev, "stream_id", None))
                if w is None:
                    continue
                if isinstance(ev, DataReceived):
                    w[1] += len(ev.data)
                if isinstance(ev, (DataReceived, HeadersReceived)) and ev.stream_ended and not w[0].done():
                    w[0].set_result(w[1])
                    self.waiters.pop(ev.stream_id, None)

    loop = asyncio.new_event_loop()

    def open_():
        cfg = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN)
        st["cm"], st["proto"] = connect(u.hostname, u.port or 443, configuration=cfg, create_protocol=H3Client), None
        try:
            st["client"] = loop.run_until_complete(asyncio.wait_for(st["cm"].__aenter__(), timeout=15))
        except Exception as e:  # noqa: BLE001 -- the cause: the server's CONNECTION_CLOSE, else what connect raised
            st["cm"] = st["client"] = None
            pr = st["proto"]
            why = (close_cause(*pr.closed) if pr is not None and pr.closed else
                   "timeout: no handshake in 15 s" if isinstance(e, asyncio.TimeoutError) else repr(e)[:120])
            raise H3Error(f"handshake to {u.hostname}: {why}") from None

    def close_():
        if st["cm"] is not None:
            try:
                loop.run_until_complete(st["cm"].__aexit__(None, None, None))
            except Exception:  # noqa: BLE001
                pass
            st["cm"] = st["client"] = None

    def fetch(n: int):
        if st["client"] is None:
            open_()
        c = st["client"]
        try:
            got = loop.run_until_complete(c.get(f"{u.path}?bytes={n}"))
        except Exception:
            close_()
            raise
        q = c._quic
        tup = None
        try:
            lo = c._transport.get_extra_info("sockname")
            peer = q._network_paths[0].addr if q._network_paths else None
            tup = {"transport": "udp", "local": norm_addr(lo[0], lo[1]),
                   "remote": norm_addr(peer[0], peer[1]) if peer else None}
        except Exception:  # noqa: BLE001
            pass
        return got, {"proto": "h3", "alpn": getattr(q.tls, "alpn_negotiated", None), "five_tuple": tup, "fallback": False}

    def shutdown():
        close_()
        loop.close()
    return fetch, close_, shutdown


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", choices=sorted(CADENCE), required=True)
    ap.add_argument("--rate-kbit", type=float, required=True, help="the state's set rate (the segment size reads it)")
    ap.add_argument("--proto", choices=("h3", "tcp"), required=True)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--fallback-url", default=FALLBACK_URL, help="TCP only: the HTTP Range fallback ('' for none)")
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--log", default=None, help="per-segment JSON lines (default stdout)")
    ap.add_argument("--probe", action="store_true",
                    help="one 10 kB fetch with --proto, its outcome as one JSON line (exit 0 ok, 5 failed): the dry "
                         "run's preflight, so an unreachable h3 endpoint fails before any phase, with its cause")
    a = ap.parse_args(argv)
    if a.probe:
        fetch, _, shutdown = h3_fetcher(a.url) if a.proto == "h3" else tcp_fetcher(a.url, None)
        try:
            got, info = fetch(10_000)
            out = {"ok": got == 10_000, "bytes": got, **info}
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": repr(e)[:300]}
        finally:
            shutdown()
        print(json.dumps({"probe": a.proto, "url": a.url, **out}))
        return 0 if out["ok"] else 5
    fh = open(a.log, "a") if a.log else sys.stdout

    def log(rec):
        fh.write(json.dumps({"platform": a.platform, **rec}) + "\n")
        fh.flush()
    cad = CADENCE[a.platform]
    if a.proto == "h3":
        fetch, reconnect, shutdown = h3_fetcher(a.url)
    else:
        fetch, reconnect, shutdown = tcp_fetcher(a.url, a.fallback_url or None)
    seg = segment_bytes(a.platform, a.rate_kbit)
    log({"start": {"proto": a.proto, "rate_kbit": a.rate_kbit, "rung_kbit": rung_kbit(a.platform, a.rate_kbit),
                   "seg_bytes": seg, **cad, "url": a.url}})
    try:
        s = Player(fetch, seg, cad["D"], cad["B"], clock=time.time, log=log,   # wall clock: the dry run's samples align
                   reconnect=reconnect if a.platform == "tiktok" else None,
                   reconnect_s=TT_RECONNECT_S if a.platform == "tiktok" else None).run(a.seconds)
    finally:
        shutdown()
    log({"summary": s})
    return 0


if __name__ == "__main__":
    sys.exit(main())
