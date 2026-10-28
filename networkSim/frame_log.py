"""t91 (boundary 3): the reference decoder of content.js's per-frame log, the frames tick row's field fl.

fl = {cs, seq, on, n, over, inexact, c, keys?}: n rows, the columns in c (FL_CODE: a column with no value in the tick is
left out; a null is a frame that lacked the field). Integers: times in us (pt / edt / ct / rt were ms, mt / pd were s).
Codes: "d" delta from the column's previous non-null value (the first absolute), "p" offset from the row's pt, "r" raw.
decode() gives one dict per frame in the browser's units (ms / s), exact to the us. The reader (LVQoE) owns its own copy;
this one is the collector's statement of the format, checked against content.js's FL_CODE by test_rvfc_frames.
"""
from __future__ import annotations

# column -> (VideoFrameCallbackMetadata field, scale to an integer, code); content.js FL_CODE, the same order
FL_CODE: dict[str, tuple[str | None, float, str]] = {
    "el": (None, 1, "d"), "pt": ("presentationTime", 1e3, "d"), "edt": ("expectedDisplayTime", 1e3, "p"),
    "mt": ("mediaTime", 1e6, "d"), "pf": ("presentedFrames", 1, "d"), "w": ("width", 1, "d"), "h": ("height", 1, "d"),
    "pd": ("processingDuration", 1e6, "r"), "ct": ("captureTime", 1e3, "p"), "rt": ("receiveTime", 1e3, "p"),
    "rtp": ("rtpTimestamp", 1, "d"),
}


def decode(fl: dict) -> list[dict]:
    """The tick's frames, one dict per row: el plus each present field under its column name, in ms / s / counts."""
    n = int(fl.get("n") or 0)
    cols = fl.get("c") or {}
    ints: dict[str, list[int | None]] = {}
    for name, (_, _, code) in FL_CODE.items():
        raw = cols.get(name)
        if raw is None:
            continue
        assert len(raw) == n, (name, len(raw), n)
        if code == "d":
            out, acc = [], 0
            for x in raw:
                if x is None:
                    out.append(None)
                    continue
                acc += x
                out.append(acc)
            ints[name] = out
        else:
            ints[name] = list(raw)
    rows = []
    for i in range(n):
        row: dict = {}
        pt_us = ints["pt"][i] if "pt" in ints else None
        for name, (_, scale, code) in FL_CODE.items():
            v = ints.get(name, [None] * n)[i]
            if v is None:
                continue
            if code == "p":
                v = v + pt_us
            row[name] = v / scale if scale != 1 else v
        rows.append(row)
    return rows
