"""offer.py: the offered ladder per session, one schema for every platform (collector v3 A4; levi 16:48 / 16:49: B3's
feasibility and the offer C5 read meta.json offer). The sources are what each player already holds, never a request:
- youtube: meta.json yt_ladder (YtLadderRecorder: the page's adaptiveFormats + getAvailableQualityLevels, on change);
- twitch: meta.json tw_ladder (MasterListener: every HLS master read, its STREAM-INF variants, on change; tw_variants);
- tiktok: meta.json tt_codec (TtCodecRecorder: the room's sdk_params renditions, on change).
offer_record() turns the first non-empty read into renditions [{name, width, height, short_side, bitrate_kbit, codecs,
fps}] (video only, sorted by bitrate, highest first) plus how often the offer changed; a session with no read says so
(missing, never an empty offer passed off as the truth)."""
from __future__ import annotations

import re

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(s: str) -> dict:
    return {k: v.strip('"') for k, v in _ATTR.findall(s or "")}


def _wh(res: str | None) -> tuple[int | None, int | None]:
    m = re.fullmatch(r"(\d+)x(\d+)", res or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def tw_variants(m3u8: str) -> list[dict]:
    """A twitch HLS master's video variants: {name (the EXT-X-MEDIA NAME of its VIDEO group), bandwidth, resolution,
    codecs, frame_rate}; audio-only variants (no RESOLUTION) are left out."""
    names = {}
    for line in (m3u8 or "").splitlines():
        if line.startswith("#EXT-X-MEDIA:"):
            a = _attrs(line.split(":", 1)[1])
            if a.get("TYPE") == "VIDEO" and a.get("GROUP-ID"):
                names[a["GROUP-ID"]] = a.get("NAME")
    out = []
    for line in (m3u8 or "").splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):
            a = _attrs(line.split(":", 1)[1])
            if not a.get("RESOLUTION"):
                continue
            out.append({"name": names.get(a.get("VIDEO")) or a.get("VIDEO"), "bandwidth": int(a.get("BANDWIDTH") or 0),
                        "resolution": a.get("RESOLUTION"), "codecs": a.get("CODECS"),
                        "frame_rate": float(a["FRAME-RATE"]) if a.get("FRAME-RATE") else None})
    return out


def _rend(name, w, h, kbit, codecs, fps) -> dict:
    return {"name": name, "width": w, "height": h, "short_side": min(w, h) if w and h else (h or w),
            "bitrate_kbit": round(kbit, 1) if kbit is not None else None, "codecs": codecs, "fps": fps}


def _yt(read: dict) -> list[dict]:
    out = []
    for f in read.get("formats") or []:
        mime = str(f.get("mimeType") or "")
        if not mime.startswith("video/"):
            continue
        m = re.search(r'codecs="([^"]*)"', mime)
        out.append(_rend(f.get("qualityLabel"), f.get("width"), f.get("height"),
                         f["bitrate"] / 1000.0 if f.get("bitrate") else None, m.group(1) if m else None, f.get("fps")))
    return out


def _tw(read: dict) -> list[dict]:
    out = []
    for v in read.get("variants") or []:
        w, h = _wh(v.get("resolution"))
        out.append(_rend(v.get("name"), w, h, v["bandwidth"] / 1000.0 if v.get("bandwidth") else None, v.get("codecs"),
                         v.get("frame_rate")))
    return out


def _tt(read: dict) -> list[dict]:
    out = []
    for r in read.get("renditions") or []:
        w, h = _wh(r.get("resolution"))
        vb = r.get("vbitrate")
        out.append(_rend(r.get("suffix"), w, h, int(vb) / 1000.0 if vb not in (None, "") else None, r.get("codec"), None))
    return out


SOURCES = {"youtube": ("yt_ladder", "page adaptiveFormats", _yt), "twitch": ("tw_ladder", "hls master", _tw),
           "tiktok": ("tt_codec", "room sdk_params", _tt)}


def offer_record(platform: str, recs: dict) -> dict:
    """meta.json offer from the platform's recorder record (recs: {"yt_ladder" | "tw_ladder" | "tt_codec": record})."""
    if platform not in SOURCES:
        return {"platform": platform, "missing": True, "why": "no offer source for this platform"}
    key, source, conv = SOURCES[platform]
    rec = recs.get(key) or {}
    reads = [r for r in rec.get("reads") or [] if conv(r)]
    base = {"platform": platform, "source": source, "from": key, "n_reads": rec.get("n_reads"),
            "rule": "collector v3 A4: the first non-empty read's video renditions; n_changes = reads that changed after it"}
    if not reads:
        return {**base, "missing": True, "renditions": [], "n_changes": 0}
    seen, rends = set(), []
    for r in conv(reads[0]):
        k = (r["name"], r["height"], r["bitrate_kbit"])
        if k not in seen:
            seen.add(k)
            rends.append(r)
    rends.sort(key=lambda r: -(r["bitrate_kbit"] or 0))
    return {**base, "missing": False, "t_first": reads[0].get("t"), "renditions": rends, "n_changes": len(reads) - 1}
