#!/usr/bin/env python3
"""t102 (levi 23:22 / 23:24, ruling (B)): tt's tier codec is the codec of the session's main player element, never
tt_stream's ladder codec (http_v2: "mixed" on 9 / 9 W1-W3 tt, so amendment 2's tier floors counted 0 sessions).

The main element: the (el, src) with the most presented frames (rVFC ticks, Δpf summed per element and src), so a
preview never wins. Its CDP media player: tt plays a worker-MSE srcObject that content.js keys by a counter
(MediaSourceHandle#n), so there is no shared key; a candidate is a player with a video codec whose natural size equals
the element's first-frame size and whose kLoad falls in [first frame - WIN_BEFORE_S, first frame + WIN_AFTER_S] (kLoad t
is the host's receive time; the first frame is the page's). codec = the one codec every candidate shares; none or a
disagreement -> null with a reason (tier_ok False). Never the first player by position. record() is meta.tt_main_codec.
"""
import json
from pathlib import Path

# off = 7809b21c (no meta.tt_main_codec; coverage_report's tier_ok reads tt_stream.codec). OFF by levi's (b) rule: on
# ti_100175 the main element's player reported hevc throughout while its 193 MB main FLV flow carries AVC (FLV video tag
# codecid 7, decrypted on halo4): the CDP player's kVideoTracks stays stale after an in-place hd5 -> hd switch.
TT_MAIN_CODEC = False
WIN_BEFORE_S = 30.0
WIN_AFTER_S = 2.0


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


def main_element(frames) -> dict | None:
    """The (el, src) with the most presented frames; its first-frame time (s) and size; None without ticks."""
    fr, last, first, dims = {}, {}, {}, {}
    for r in frames:
        key = (r.get("el"), r.get("src"))
        if r.get("k") == "first_frame" and r.get("w"):
            first.setdefault(key, r["ts"] / 1000.0)
            dims.setdefault(key, (r["w"], r["h"]))
        if r.get("k") != "tick" or r.get("pf") is None:
            continue
        if key in last and r["pf"] >= last[key]:
            fr[key] = fr.get(key, 0) + r["pf"] - last[key]
        last[key] = r["pf"]
    if not fr:
        return None
    key = max(fr, key=fr.get)
    return {"element": list(key), "frames": fr[key], "first_t": first.get(key), "size": dims.get(key)}


def players(media_events) -> dict:
    """playerId -> {t_load (host s), codec (first kVideoTracks), sizes (every natural size it reported)}."""
    out: dict = {}
    for r in media_events:
        x = out.setdefault(r.get("playerId"), {"t_load": None, "codec": None, "sizes": []})
        if r.get("method") == "playerEventsAdded":
            for e in r.get("events") or []:
                try:
                    v = json.loads(e.get("value") or "{}")
                except (ValueError, TypeError):
                    continue
                if isinstance(v, dict) and v.get("event") == "kLoad" and x["t_load"] is None:
                    x["t_load"] = r.get("t")
        elif r.get("method") == "playerPropertiesChanged":
            for q in r.get("properties") or []:
                if q.get("name") != "kVideoTracks":
                    continue
                try:
                    tr = (json.loads(q.get("value") or "[]") or [{}])[0]
                except (ValueError, TypeError, IndexError):
                    continue
                x["codec"] = x["codec"] or tr.get("codec")
                if tr.get("natural size") and tr["natural size"] not in x["sizes"]:
                    x["sizes"].append(tr["natural size"])
    return out


def main_codec(frames, media_events) -> dict:
    rec = {"source": "media_decoders", "rule": "t102 (B) levi 23:24", "window_s": [-WIN_BEFORE_S, WIN_AFTER_S]}
    m = main_element(frames)
    if m is None:
        return {**rec, "codec": None, "why": "no rVFC ticks"}
    rec.update(element=m["element"], frames=m["frames"])
    if m["first_t"] is None or m["size"] is None:
        return {**rec, "codec": None, "why": "no first frame on the main element"}
    want = f"{m['size'][0]}x{m['size'][1]}"
    cand = []
    for pid, p in players(media_events).items():
        if not p["codec"] or p["t_load"] is None or want not in p["sizes"]:
            continue
        dt = round(m["first_t"] - p["t_load"], 3)          # > 0: the load preceded the first frame
        if -WIN_AFTER_S <= dt <= WIN_BEFORE_S:
            cand.append({"player": pid, "codec": p["codec"], "size": want, "dt": dt})
    rec.update(candidates=cand, agree=len({c["codec"] for c in cand}) == 1)
    if not cand:
        return {**rec, "codec": None, "why": f"no {want} video player loaded in the window"}
    if not rec["agree"]:
        return {**rec, "codec": None, "why": "candidates disagree"}
    return {**rec, "codec": cand[0]["codec"]}


def record(session_dir: Path, frames_file: str, media_file: str) -> dict:
    return main_codec(_rows(session_dir / frames_file), _rows(session_dir / media_file))


def tier_ok(meta: dict) -> bool:
    """coverage_report's tt tier rule (amendment 2, the codec per levi 23:22 (B))."""
    if TT_MAIN_CODEC:
        return (meta.get("tt_main_codec") or {}).get("codec") == "hevc"
    return ((meta.get("tt_stream") or {}).get("codec") or "unknown") == "hevc"
