#!/usr/bin/env python3
"""t102 (B') (levi 23:34): tt's tier codec is the codec covering most of the main element's presented frames, from
WorkerAnchor's worker-MSE rows, never tt_stream's ladder codec ("mixed" on 9 / 9 W1-W3 tt, so amendment 2's tier floors
counted 0 sessions) and never CDP media_decoders (ruling (B), OFF: kVideoTracks stays stale after tt's in-place
hd5 -> hd switch, ti_100175 decrypt-verified).

The main element: the (el, src) with the most presented frames (rVFC ticks, Δpf summed per element and src). Previews are
MediaStream elements and have no MSE, so every worker video init / changeType / add row belongs to an MSE player. The
codec timeline: each worker row's video codec (init: the 'vide' track's sample entry; ct / add: the mime's codecs=) at
its epoch time (the worker's time_origin + t, meta frame_log.worker_anchor.workers). Each main-element tick interval's
presented frames go to the codec in force at the interval's start; the codec is the one with the most frames.
record() is meta.tt_main_codec; tier_ok() is coverage_report's tt tier rule.
"""
import json
from pathlib import Path

# off = 7809b21c (no meta.tt_main_codec; coverage_report's tier_ok reads tt_stream.codec). levi 23:34 (3): ON only with
# 9 / 9 W1-W3 tt agreeing with the decrypted FLV video-tag codec of the main span's connections.
TT_MAIN_CODEC = False
ENTRY_CODEC = {"hvc1": "hevc", "hev1": "hevc", "avc1": "h264", "avc3": "h264"}
SWITCHES_KEPT = 50


def _rows(path: Path):
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _entry(s) -> str | None:
    return ENTRY_CODEC.get(str(s or "")[:4].lower())


def row_codec(r: dict) -> str | None:
    """A worker MSE row's video codec: init -> the 'vide' track's sample entry; ct / add -> the video mime's codecs=."""
    if r.get("op") == "init":
        return next((_entry(t[3]) for t in r.get("tr") or [] if len(t) > 3 and t[2] == "vide"), None)
    if r.get("op") in ("ct", "add"):
        mime = str(r.get("mime") or "")
        if mime.startswith("video/") and "codecs=" in mime:
            return _entry(mime.split("codecs=", 1)[1].strip('"\' '))
    return None


def main_element(frames) -> dict | None:
    """The (el, src) with the most presented frames and its tick intervals [(t start s, Δpf)]; None without ticks."""
    fr, last, iv = {}, {}, {}
    for r in frames:
        if r.get("k") != "tick" or r.get("pf") is None or r.get("ts") is None:
            continue
        key = (r.get("el"), r.get("src"))
        if key in last and r["pf"] >= last[key][1]:
            d = r["pf"] - last[key][1]
            fr[key] = fr.get(key, 0) + d
            iv.setdefault(key, []).append((last[key][0], d))
        last[key] = (r["ts"] / 1000.0, r["pf"])
    if not fr:
        return None
    key = max(fr, key=fr.get)
    return {"element": list(key), "frames": fr[key], "intervals": iv.get(key, [])}


def timeline(frames, workers: list) -> list:
    """[(epoch s, codec, op, wi, sb)] of the worker video rows, sorted by time."""
    origin = {w.get("wi"): w.get("time_origin") for w in workers or [] if w.get("time_origin") is not None}
    out = []
    for r in frames:
        if r.get("k") != "mse" or "wi" not in r or r.get("wi") not in origin or r.get("t") is None:
            continue
        c = row_codec(r)
        if c:
            out.append((round((origin[r["wi"]] + r["t"]) / 1000.0, 3), c, r.get("op"), r.get("wi"), r.get("sb")))
    return sorted(out)


def main_codec(frames: list, workers: list) -> dict:
    rec = {"source": "worker_anchor", "rule": "t102 (B') levi 23:34"}
    m = main_element(frames)
    if m is None:
        return {**rec, "codec": None, "why": "no rVFC ticks"}
    rec.update(element=m["element"], frames=m["frames"])
    tl = timeline(frames, workers)
    if not tl:
        return {**rec, "codec": None, "why": "no worker video init / changeType rows"}
    by: dict = {}
    i, cur = 0, None
    for t, d in m["intervals"]:                               # both sorted by time
        while i < len(tl) and tl[i][0] <= t:
            cur = tl[i][1]
            i += 1
        k = cur or "before_first_init"
        by[k] = by.get(k, 0) + d
    sw = [list(x[:3]) for j, x in enumerate(tl) if j == 0 or x[1] != tl[j - 1][1]]
    codec = max(by, key=by.get)
    rec.update(frames_by_codec=by, switches=sw[:SWITCHES_KEPT], switches_n=len(sw))
    if codec == "before_first_init":
        return {**rec, "codec": None, "why": "most frames precede every worker video row"}
    return {**rec, "codec": codec}


def record(session_dir: Path, frames_file: str) -> dict:
    try:
        meta = json.loads((session_dir / "meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    workers = ((meta.get("frame_log") or {}).get("worker_anchor") or {}).get("workers") or []
    return main_codec(list(_rows(session_dir / frames_file)), workers)


def tier_ok(meta: dict) -> bool:
    """coverage_report's tt tier rule (amendment 2; the codec per levi 23:34 (B'))."""
    if TT_MAIN_CODEC:
        return (meta.get("tt_main_codec") or {}).get("codec") == "hevc"
    return ((meta.get("tt_stream") or {}).get("codec") or "unknown") == "hevc"
