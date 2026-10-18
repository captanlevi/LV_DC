"""tt_stream.py: the TikTok LIVE stream regime of a session, from its http_v2.json (supervisor 2026-10-18; coverage
diagnosis: chaos Chrome reported no HEVC, so tt served its H.264 stream, whose ladder is one rung, hd).

The room page's player data lists every rendition with its sdk_params (VCodec h264 / h265, stream_suffix, resolution);
the FLV requests name the rendition played (stream-<id>_<suffix>.flv). Recorded per session in meta.json tt_stream:
- requested: FLV suffix -> requests (audio-only requests left out);
- codec: the requested renditions' codec (h264 / hevc / mixed / unknown), per suffix in codec_of; from the room page's
  sdk_params, else (no room page captured) the suffix convention, listed in codec_inferred_from_suffix;
- ladder: per codec, the renditions the room offered (suffix, resolution), audio-only left out;
- show_quality_button: the values the room pages carried.
So the H.264 single-rung sessions so far stay a recorded regime beside the HEVC ones (levi: a covariate, addendum Q).
"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

FLV_REQ = re.compile(r"stream-\d+(?:_([A-Za-z0-9]+))?\.flv")
ROOM_PATH = re.compile(r"^/@[^/]+/live|/webcast/room/")
CODEC = {"h264": "h264", "h265": "hevc", "hevc": "hevc", "bytevc1": "hevc"}


def _blocks(t: str, key: str = '"sdk_params":"{'):
    """Each sdk_params object (brace-matched; the page's JSON-in-JSON with the backslashes removed)."""
    i = t.find(key)
    while i >= 0:
        j, depth = i + len(key) - 1, 0
        for k in range(j, min(len(t), j + 4000)):
            depth += {"{": 1, "}": -1}.get(t[k], 0)
            if depth == 0:
                yield t[j:k + 1]
                break
        i = t.find(key, i + 1)


def renditions(text: str) -> dict[str, dict]:
    """suffix -> {codec, resolution} from a room page's sdk_params."""
    t = text.replace("\\", "")
    out = {}
    for b in _blocks(t):
        vc = re.search(r'"VCodec":"(\w+)"', b)
        sx = re.search(r'"stream_suffix":"(\w*)"', b)
        rs = re.search(r'"resolution":"([0-9x]*)"', b)
        if not (vc and sx) or sx.group(1) in ("ao", ""):
            continue
        out[sx.group(1)] = {"codec": CODEC.get(vc.group(1).lower(), vc.group(1).lower()),
                            "resolution": rs.group(1) if rs else None}
    return out


def suffix_codec(suffix: str) -> str:
    """The suffix convention seen in the room data (chaos and the old sessions): H.264 renditions are plain (hd, sd, ld,
    or4, uhd), HEVC ones carry a 5 (hd5, sd5, ld5, sld520, hd560, uhd560); anything else unknown."""
    if re.fullmatch(r"(uhd|hd|sd|ld|or4|origin)", suffix):
        return "h264"
    if re.fullmatch(r"[a-z]+5\d*", suffix):
        return "hevc"
    return "unknown"


def regime(http_v2: dict) -> dict:
    requested, rend, buttons, pages = collections.Counter(), {}, set(), 0
    for conn, es in http_v2.items():
        for e in es:
            u = e.get("request_url") or ""
            m = FLV_REQ.search(u.split("?")[0])
            if m and "only_audio=1" not in u:
                requested[m.group(1) or ""] += 1
            rd = e.get("response_data")
            path = urlsplit(u).path if u.startswith("http") else u.split("?")[0]    # http_v2 stores paths
            if isinstance(rd, str) and len(rd) > 5000 and ROOM_PATH.search(path):
                pages += 1
                rend.update(renditions(rd))
                buttons.update(re.findall(r'"show_quality_button":(true|false)', rd.replace("\\", "")))
    codec_of, inferred = {}, []
    for s in requested:
        if s in rend:
            codec_of[s] = rend[s]["codec"]
        else:                                     # no room page captured: the suffix convention, marked inferred
            codec_of[s] = suffix_codec(s)
            inferred.append(s)
    codecs = set(codec_of.values())
    ladder = collections.defaultdict(list)
    for s, r in sorted(rend.items(), key=lambda kv: kv[0]):
        ladder[r["codec"]].append([s, r["resolution"]])
    return {"source": "http_v2", "room_pages": pages, "requested": dict(requested), "codec_of": codec_of,
            "codec_inferred_from_suffix": sorted(inferred),
            "codec": (codecs.pop() if len(codecs) == 1 else "mixed") if codecs else "unknown",
            "ladder": dict(ladder), "show_quality_button": sorted(buttons)}


def tt_stream_regime(session_dir) -> dict:
    p = Path(session_dir) / "http_v2.json"
    if not p.exists():
        return {"source": "http_v2", "error": "no http_v2.json"}
    try:
        return regime(json.loads(p.read_text()))
    except Exception as e:  # never fatal for a session
        return {"source": "http_v2", "error": repr(e)[:200]}
