"""Per-format parsers for the trace-driven held-out regime's public throughput traces (LVQoE restart prereg §9): each
returns a per-second throughput series in kbit/s (index i = second i of the trace), so the regime's shaper rate is
series[offset + t]. None of the three sources carries an RTT column, so every parser returns rtt_ms None (the regime
then uses the fixed 40 ms x the per-session offset, §9).

 - Riiser et al. 3G / HSDPA (Norway): whitespace columns: unix s, a monotonic ms clock, lat, lon, bytes received since
   the previous row, ms since the previous row (= the ms clock's difference); the timeline is column 6's running sum.
 - van der Hooft et al. 4G / LTE (Ghent): the same six columns, the first in ms since the epoch.
 - FCC MBA raw release, curr_webget (web browsing): one row per page fetch (unit_id, dtime, target, address, fetch_time,
   bytes_total, bytes_sec, ...); a trace is one (unit_id, target)'s fetches in time order, each fetch's bytes_sec held
   FCC_HOLD_S seconds (Pensieve's construction, Mao et al. SIGCOMM 2017: traces/fcc/load_webget_data.py and the 5 s
   step of convert_mahimahi_format.py).
"""
from __future__ import annotations

import csv
import io
import math

PARSER_VERSION = "1"   # bump on any change that alters a derived series
FCC_HOLD_S = 5                 # Pensieve: each webget value is one 5 s step
FCC_BYTES_SEC_COL = 6          # curr_webget's bytes_sec (Pensieve's parse[6])


def _bytes_ms_rows(text: str) -> list[tuple[float, float, float]]:
    """(end ms, bytes, interval ms) per row of a six-column Riiser / Ghent log; a short or non-numeric row is skipped.
    The timeline is the cumulative sum of the rows' intervals (column 6) in time order, never the column-2 clock: one
    published 3G log runs in reverse time order (column 1 decreasing: reversed here) and one has a garbled column 2
    after an outage while its column 6 stays consistent with column 1 (car.oslo-grimstad 2011-04-21)."""
    rows = []
    for ln in text.splitlines():
        f = ln.split()
        if len(f) < 6:
            continue
        try:
            t, b, dt_ms = float(f[0]), float(f[4]), float(f[5])
        except ValueError:
            continue
        rows.append((t, b, dt_ms))
    if len(rows) > 1 and rows[0][0] > rows[-1][0]:
        rows.reverse()
    out, clock = [], 0.0
    for _, b, dt_ms in rows:
        clock += max(dt_ms, 0.0)
        out.append((clock, b, dt_ms))
    return out


def per_second_from_intervals(rows: list[tuple[float, float, float]]) -> list[float]:
    """Rows (end clock ms, bytes, interval ms) to a per-second kbit/s series on a 1 s grid from the first interval's
    start: each interval's bytes spread uniformly over it, each second's bytes summed (a gap between intervals is 0);
    the last partial second is dropped. An interval of <= 0 ms puts its bytes at its end instant."""
    if not rows:
        return []
    t0 = rows[0][0] - max(rows[0][2], 0.0)
    end = rows[-1][0]
    n = int(math.floor((end - t0) / 1000.0))
    bins = [0.0] * max(n, 0)
    for clock, b, dt in rows:
        s = clock - max(dt, 0.0)
        if dt <= 0:
            k = int((clock - t0) // 1000.0)
            if 0 <= k < n:
                bins[k] += b
            continue
        rate = b / dt                                   # bytes per ms
        a = s
        while a < clock:
            k = int((a - t0) // 1000.0)
            if k >= n:
                break
            edge = min(clock, t0 + (k + 1) * 1000.0)
            if k >= 0:
                bins[k] += rate * (edge - a)
            a = edge
    return [x * 8.0 / 1000.0 for x in bins]             # bytes per s -> kbit/s


def parse_riiser(text: str) -> dict:
    """A Riiser 3G / HSDPA log to {kbps: per-second series, rtt_ms: None, rows}."""
    rows = _bytes_ms_rows(text)
    return {"kbps": per_second_from_intervals(rows), "rtt_ms": None, "rows": len(rows)}


def parse_ghent(text: str) -> dict:
    """A van der Hooft 4G / LTE log (the same six columns; column 1 in ms) to the same record."""
    rows = _bytes_ms_rows(text)
    return {"kbps": per_second_from_intervals(rows), "rtt_ms": None, "rows": len(rows)}


def fcc_webget_series(lines, keys: set | None = None) -> dict[tuple[str, str], list[float]]:
    """curr_webget rows (an iterable of CSV lines, a header row skipped) to {(unit_id, target): [bytes_sec in time
    order]}; rows whose bytes_sec is empty or non-numeric are dropped; keys: keep only these series."""
    out: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for r in csv.reader(lines):
        if len(r) <= FCC_BYTES_SEC_COL:
            continue
        try:
            v = float(r[FCC_BYTES_SEC_COL])
        except ValueError:
            continue                                     # the header, or a failed fetch with no bytes_sec
        k = (r[0], r[2])
        if keys is not None and k not in keys:
            continue
        out.setdefault(k, []).append((r[1], v))
    return {k: [v for _, v in sorted(rows, key=lambda x: x[0])] for k, rows in out.items()}


def fcc_webget_counts(lines) -> dict[tuple[str, str], int]:
    """The values per (unit_id, target) series under fcc_webget_series' rule, without holding them (the first pass)."""
    out: dict[tuple[str, str], int] = {}
    for r in csv.reader(lines):
        if len(r) <= FCC_BYTES_SEC_COL:
            continue
        try:
            float(r[FCC_BYTES_SEC_COL])
        except ValueError:
            continue
        out[(r[0], r[2])] = out.get((r[0], r[2]), 0) + 1
    return out


def fcc_per_second(bytes_sec: list[float], hold_s: int = FCC_HOLD_S) -> dict:
    """One FCC series to the per-second record: each value held hold_s seconds, bytes/s -> kbit/s."""
    return {"kbps": [v * 8.0 / 1000.0 for v in bytes_sec for _ in range(hold_s)], "rtt_ms": None, "rows": len(bytes_sec)}


def series_text(kbps: list[float]) -> str:
    """The canonical text of a per-second series (one kbit/s value per line, 3 decimals): what a derived trace's
    sha256 is taken over."""
    return "".join(f"{v:.3f}\n" for v in kbps)


def parse_fcc_csv_text(text: str) -> dict[tuple[str, str], list[float]]:
    return fcc_webget_series(io.StringIO(text))
